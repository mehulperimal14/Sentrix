"""One SENTRIX product in one process, for the end-to-end tests.

* **Portal**: ``sentrix_portal.create_app`` on a temporary SQLite database
  (``PortalSettings.for_tests``), no background workers. Its notification
  router has a :class:`FakeTransport` on Telegram and e-mail; the worker is
  driven by hand (:meth:`World.pump_portal`), and its delivery feedback
  (``NotifyWorker.sos_feedback``) is wired as in production.
* **Device**: the Local Console app (``sentrix.app.create_app``, no lifespan:
  no pipeline, no camera, no microphone) with a fake pipeline, the real SOS
  engine (``sentrix.sos.service.SosEngine``) on an injectable clock, a device
  channel router whose modem is a :class:`FakeTransport`, and the real link
  client (``LinkService``).
* **Link**: ``test_link_support.MemoryNetwork``: an in-memory pipe whose
  Portal end is served by the Portal's real hub. The hub, the link client
  and every Portal HTTP request run on ONE event loop thread
  (:class:`LoopThread`), so futures never cross loops.
* **Browsers** (:class:`PortalBrowser`): ``httpx.AsyncClient`` over
  ``ASGITransport`` into the Portal app, run on that loop.

Nothing leaves the process: the conftest socket guard is on, every transport
is a fake, and the device database is the ``temp_db`` fixture.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

PASSWORD = "correct horse battery"
LC_PASSWORD = "lc device admin pass"
SOS_PIN = "482916"
DURESS_PIN = "482971"
OWNER_PHONE = "+919812345610"
OWNER_EMAIL = "riya@example.com"
OWNER_CHAT = 7770001
CONTACT_PHONE = "+919876543210"
CONTACT_EMAIL = "arjun@example.com"
DESK_PHONE = "+919812345699"

CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')
#: A claim / setup code (8 letters), not a piece of a 12-letter activation code.
CODE8 = re.compile(r"(?<![A-Z-])[B-DF-HJ-NP-TV-XZ]{4}-[B-DF-HJ-NP-TV-XZ]{4}(?![A-Z-])")
DOM_ID = re.compile(r'\sid="([^"]+)"')


def wait_for(condition, timeout: float = 10.0, interval: float = 0.01, what: str = ""):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what or condition}")
        time.sleep(interval)


# ── one event loop for the hub, the link client and the Portal requests ──────
class LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                       name="e2e-loop")
        self.thread.start()

    def run(self, coro, timeout: float = 20.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        async def cancel_others():
            me = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not me]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self.run(cancel_others(), timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        if not self.loop.is_running():
            self.loop.close()


class PortalBrowser:
    """A browser on the Portal: cookies, the page's CSRF token on every form."""

    def __init__(self, world: "World", ip: str = "198.51.100.7") -> None:
        self.world = world
        transport = httpx.ASGITransport(app=world.app, client=(ip, 50000))
        self.client = httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False,
            headers={"accept": "text/html,application/xhtml+xml"}, timeout=30)
        self.csrf: str | None = None
        world.browsers.append(self)

    def _remember(self, response) -> None:
        if "text/html" in response.headers.get("content-type", ""):
            found = CSRF_META.search(response.text) or CSRF_INPUT.search(response.text)
            if found:
                self.csrf = found.group(1)

    def request(self, method: str, path: str, **kwargs):
        response = self.world.loop.run(self.client.request(method, path, **kwargs))
        self._remember(response)
        return response

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, data: dict | None = None, *, csrf: bool = True, **kwargs):
        body = dict(data or {})
        if csrf and self.csrf and "csrf_token" not in body:
            body["csrf_token"] = self.csrf
        return self.request("POST", path, data=body, **kwargs)

    def form(self, path: str, data: dict | None = None, **kwargs):
        """GET the page (for its token), then POST to it."""
        self.get(path)
        return self.post(path, data, **kwargs)

    def api(self, method: str, path: str, **kwargs):
        """A page script's fetch: JSON, the CSRF header."""
        headers = {"accept": "application/json", **kwargs.pop("headers", {})}
        if self.csrf and method.upper() != "GET":
            headers["X-CSRF-Token"] = self.csrf
        return self.request(method, path, headers=headers, **kwargs)

    def login(self, username: str, password: str = PASSWORD):
        self.get("/login")
        return self.post("/login", {"username": username, "password": password})

    def login_staff(self, username: str, secret: str, password: str = PASSWORD):
        import pyotp
        from sentrix_portal import security
        self.login(username, password)
        security._totp_last_step.clear()
        self.get("/account/2fa")
        return self.post("/account/2fa", {"action": "verify",
                                          "code": pyotp.TOTP(secret).now()})

    async def aclose(self) -> None:
        await self.client.aclose()


# ── the device ────────────────────────────────────────────────────────────────
class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


class FakeEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def audit(self, event, **detail):
        self.rows.append((event, detail))

    def names(self) -> list[str]:
        return [name for name, _ in self.rows]


class DevicePipeline:
    """What the console, the link and the SOS engine read from the pipeline
    (no models, no camera)."""

    def __init__(self) -> None:
        import numpy as np
        from sentrix.io.actuators import ConsoleSiren
        self.session_id = "e2e-session"
        self.source = SimpleNamespace(online=True, name="file:demo.mp4")
        self.detector = SimpleNamespace(available=True)
        self.scene = SimpleNamespace(weights_name="scene.pt", available=False, device=None,
                                     thresholds=None)
        self.violence = None
        self.identity = SimpleNamespace(backend=None, enrolled={}, enrolled_files=set())
        self.audio = None
        self.tci = SimpleNamespace(model_source="evidence")
        self.siren = ConsoleSiren()
        self.siren_calls: list = []
        self.siren.activate = lambda duration=None: self.siren_calls.append(duration)
        self.events = FakeEvents()
        self.incidents = SimpleNamespace(current=None)
        self.closed: list[str] = []
        self.frame = np.zeros((48, 64, 3), np.uint8)
        self._seq = 0

    @property
    def preview_seq(self) -> int:
        self._seq += 1
        return self._seq

    def latest_raw(self):
        return self.frame

    def latest_jpeg(self, quality: int = 70):
        import cv2
        ok, buffer = cv2.imencode(".jpg", self.frame)
        return self._seq, buffer.tobytes()

    def request_close_incident(self, reason: str = "demo") -> bool:
        self.closed.append(reason)
        return True


def device_policy():
    """The device's send guard: test mode, both switches on, the household's
    numbers allowlisted (``SENTRIX_TEST_RECIPIENTS``). Only fakes receive."""
    from sentrix.alerting import guard
    return guard.SendPolicy(test_mode=True, live_alerts=True,
                            switches={ch: True for ch in guard.CHANNEL_SWITCHES},
                            allowlist=frozenset({OWNER_PHONE, CONTACT_PHONE, DESK_PHONE}))


def portal_policy():
    from sentrix.alerting import guard
    return guard.SendPolicy(test_mode=True, live_alerts=True,
                            switches={ch: True for ch in guard.CHANNEL_SWITCHES},
                            allowlist=frozenset({OWNER_EMAIL, f"tg:{OWNER_CHAT}",
                                                 CONTACT_EMAIL}))


class DeviceChannels:
    """The device ``channels`` service over the real router; the modem is a
    :class:`FakeTransport` (SMS and call)."""

    name = "channels"

    def __init__(self, clock: Clock) -> None:
        from sentrix.alerting.channels.base import (Channel, ChannelRouter, FakeTransport,
                                                    MemoryOutbox)
        self.transports = {n: FakeTransport(n) for n in ("modem_sms", "modem_call")}
        self.router = ChannelRouter({n: Channel(n, t, policy_factory=device_policy)
                                     for n, t in self.transports.items()},
                                    MemoryOutbox(), clock=clock)

    def enqueue(self, recipient, message):
        return self.router.enqueue(recipient, message)

    def ready(self, channel):
        return self.router.ready(channel)

    def add_listener(self, fn):
        self.router.add_listener(fn)

    def remove_listener(self, fn):
        self.router.remove_listener(fn)

    def status(self):
        return self.router.status()

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {}

    def sms(self) -> list[tuple[str, str, str]]:
        """(address, step, text) of every SMS the fake modem sent."""
        return [(r.address, m.step, m.text) for r, m in self.transports["modem_sms"].sent]


def device_settings(portal_url: str):
    from sentrix.config import settings
    copy = SimpleNamespace(**{f: getattr(settings, f) for f in settings.__dataclass_fields__})
    copy.sos = replace(settings.sos, enabled=True)
    copy.link = replace(settings.link, enabled=True, portal_url=portal_url,
                        heartbeat_seconds=0.5)
    copy.test_mode = settings.test_mode
    return copy


# ── the world ─────────────────────────────────────────────────────────────────
@dataclass
class World:
    tmp: Path
    monkeypatch: Any
    app: Any
    loop: LoopThread
    network: Any
    lc: Any
    tg: Any
    mail: Any
    feedback: Any
    browsers: list = field(default_factory=list)
    clock: Clock = field(default_factory=Clock)
    rig: Any = None
    engine: Any = None
    channels: Any = None
    pipeline: Any = None
    facts: dict = field(default_factory=dict)

    # ── Portal ────────────────────────────────────────────────────────────────
    def browser(self, ip: str = "198.51.100.7") -> PortalBrowser:
        return PortalBrowser(self, ip)

    def db(self):
        return self.app.state.db.session()

    def pump_portal(self, rounds: int = 3) -> int:
        """Deliver what the Portal queued (the notification worker's job)."""
        from sentrix_portal.services import notify
        sent = 0
        for _ in range(rounds):
            sent += notify.worker_router(self.app.state.db).pump()
        return sent

    def portal_messages(self, transport=None) -> list[tuple[str, str, str, str]]:
        """(address, template, step, text) the fakes received."""
        out = []
        for t in ([transport] if transport else [self.tg, self.mail]):
            out += [(r.address, m.template, m.step, m.text) for r, m in t.sent]
        return out

    def notifications(self, request_id: str | None = None) -> list:
        from sentrix_portal.models import Notification
        with self.db() as db:
            query = db.query(Notification)
            if request_id:
                query = query.filter_by(request_id=request_id)
            rows = query.all()
            db.expunge_all()
            return rows

    def mirror(self, request_id: str):
        from sentrix_portal.models import AssistanceRequest
        with self.db() as db:
            row = db.get(AssistanceRequest, request_id)
            if row is not None:
                db.expunge(row)
            return row

    def mirror_state(self, request_id: str) -> str | None:
        row = self.mirror(request_id)
        return row.state if row else None

    def online(self) -> bool:
        from sentrix_portal.services import hub
        return bool(self.facts.get("device_id")) and hub.is_online(self.facts["device_id"])

    def check_heartbeats(self, later: float):
        from datetime import timedelta
        from sentrix_portal.models import utcnow
        from sentrix_portal.services.hub import hub_for
        return self.loop.run(hub_for(self.app).check_heartbeats(
            now=utcnow() + timedelta(seconds=later)))

    def run_cloud_timers(self, later: float) -> int:
        from datetime import timedelta
        from sentrix_portal.models import utcnow
        from sentrix_portal.services import sos as portal_sos
        with self.db() as db:
            return portal_sos.run_timers(db, now=utcnow() + timedelta(seconds=later))

    # ── device ────────────────────────────────────────────────────────────────
    def install_device(self, identity: dict) -> None:
        """Flash the provisioned identity onto a device and power it on."""
        from sentrix import services
        from sentrix.sos import pins
        from sentrix.sos.service import SosEngine
        from test_link_support import make_device
        rig = make_device(self.tmp / "device", self.monkeypatch, connector=self.network,
                          device_id=identity["device_id"], app=self.lc,
                          loop=self.loop.loop)
        (rig.identity_dir / "identity.json").write_text(json.dumps(identity), "utf-8")
        pipeline = DevicePipeline()
        from sentrix.pipeline import engine as engine_module
        self.monkeypatch.setattr(engine_module, "_pipeline", pipeline)
        rig.ctx.get_pipeline = lambda: pipeline
        channels = DeviceChannels(self.clock)
        sos = SosEngine(device_settings("http://portal.test"), clock=self.clock,
                        state=rig.state, channels=lambda: channels,
                        pipeline=lambda: pipeline,
                        limiter=pins.PinLimiter(5, 600, clock=self.clock),
                        snapshot_dir=self.tmp / "snapshots")
        sos._sweeper.start = lambda: None            # the tests drive work()
        services.registry.register(channels, "channels")
        services.registry.register(sos, "sos")
        rig.ctx.registry.register(channels, "channels")
        sos.start()
        self.rig, self.engine, self.channels, self.pipeline = rig, sos, channels, pipeline
        rig.service.start()

    def tick(self, seconds: float = 0.0) -> None:
        """Advance the device clock and let the SOS engine work the way its
        sweeper thread does: again after at least 50 ms while a timer is due
        (a step due "at once" lands on the next pass)."""
        self.clock.now += seconds
        for _ in range(5):
            next_in = self.engine.work()
            if next_in is None or next_in > 0.05:
                break
            self.clock.now += 0.05
        self.channels.router.pump()

    def new_incident(self, kind: str = "weapon", level: int = 3) -> str:
        from sentrix.pipeline.incidents import IncidentManager
        from sentrix.storage import database
        incident, _ = IncidentManager().update(level, 0.5, kind, now=0.0, features={},
                                               reason=f"{kind} confirmed",
                                               trigger={kind: {"state": "confirmed"}})
        database.save_incident(incident, self.pipeline.session_id, ongoing=True)
        return incident.id

    def trigger(self, kind: str = "weapon", level: int = 3, evidence: str = "confirmed",
                incident_id: str | None = None) -> str:
        """What the pipeline does at a committed L3 of an alarm class."""
        from sentrix.sos import SosTrigger
        incident_id = incident_id or self.new_incident(kind, level)
        assert self.engine.wants(kind, level)
        self.engine.trigger(SosTrigger(incident_id=incident_id, level=level,
                                       incident_type=kind, evidence_state=evidence,
                                       tci=0.6 if level == 3 else 0.9,
                                       reason=f"{kind} {evidence}",
                                       session_id=self.pipeline.session_id,
                                       at_wall=self.clock()))
        self.tick()
        return incident_id

    def open_request(self) -> dict | None:
        from sentrix.storage import requests
        return requests.open_request()

    def device_request(self, request_id: str) -> dict:
        from sentrix.storage import requests
        return requests.get_request(request_id)

    def go_offline(self) -> None:
        """The uplink is pulled: the pipe drops and dialling fails."""
        self.network.refuse = True
        self.network.drop()
        wait_for(lambda: not (self.rig.state.get().get("link") or {}).get("connected"),
                 what="the device sees the link down")
        wait_for(lambda: not self.online(), what="the hub sees the device leave")

    def go_online(self) -> None:
        self.network.refuse = False
        self.rig.service.reconnect_now()
        wait_for(self.online, timeout=15, what="the device back online")
        wait_for(lambda: (self.rig.state.get().get("link") or {}).get("connected"),
                 what="the device sees the link up")

    # ── teardown ──────────────────────────────────────────────────────────────
    def close(self) -> None:
        if self.rig is not None:
            self.rig.service.stop()
        if self.engine is not None:
            self.engine.stop()
        for browser in self.browsers:
            try:
                self.loop.run(browser.aclose(), timeout=5)
            except Exception:
                pass
        self.loop.close()


def build_world(tmp_path: Path, monkeypatch) -> World:
    """A Portal and the pieces of a device, not yet provisioned."""
    from sentrix.alerting.channels.base import FakeTransport
    from sentrix.pipeline.state import state
    from sentrix_portal import security
    from sentrix_portal.app import create_app
    from sentrix_portal.services import hub, notify
    from sentrix_portal.services import sos as portal_sos
    from sentrix_portal.settings import PortalSettings
    from sentrix.config import settings
    from test_link_support import MemoryNetwork, lc_app

    hub.reset()
    notify.reset()
    portal_sos.reset_pin_limiter()
    security._totp_last_step.clear()
    state.reset()
    monkeypatch.setenv("SENTRIX_PASSWORD", LC_PASSWORD)
    monkeypatch.setattr(hub, "_cfg", lambda: replace(
        settings, link=replace(settings.link, heartbeat_seconds=0.5)))
    app = create_app(PortalSettings.for_tests(tmp_path / "portal", desk_number=DESK_PHONE))
    tg, mail = FakeTransport("telegram"), FakeTransport("email")
    notify.configure(transports={"telegram": tg, "email": mail}, policy_factory=portal_policy)
    feedback = notify.NotifyWorker(app)
    notify.add_listener(feedback.sos_feedback)
    loop = LoopThread()
    network = MemoryNetwork(app)
    network._loop = loop.loop
    world = World(tmp=tmp_path, monkeypatch=monkeypatch, app=app, loop=loop,
                  network=network, lc=lc_app(monkeypatch), tg=tg, mail=mail,
                  feedback=feedback)
    return world


def teardown_world(world: World) -> None:
    from sentrix.pipeline.state import state
    from sentrix_portal.services import hub, notify
    world.close()
    notify.remove_listener(world.feedback.sos_feedback)
    notify.reset()
    hub.reset()
    state.reset()
    world.app.state.db.dispose()


# ── product steps ─────────────────────────────────────────────────────────────
def add_staff(world: World, *, username: str, role: str) -> str:
    """A staff account with TOTP (what the bootstrap tool makes); returns the
    TOTP secret."""
    import pyotp
    from test_acct_support import add_user
    secret = pyotp.random_base32()
    add_user(world.app, username=username, role=role, totp_secret=secret)
    return secret


def staff(world: World, username: str, role: str) -> PortalBrowser:
    secret = add_staff(world, username=username, role=role)
    browser = world.browser(ip="198.51.100.20")
    response = browser.login_staff(username, secret)
    assert response.status_code == 303, response.text[:300]
    return browser


def parse_identity(label_html: str) -> dict:
    raw = re.search(r'id="identity-json">(.*?)</pre>', label_html, re.S).group(1)
    return json.loads(raw.replace("&#34;", '"').replace("&quot;", '"'))


def provision(world: World, admin: PortalBrowser, *, username: str = "riya",
              name: str = "Riya Sharma", email: str = OWNER_EMAIL,
              phone: str = "98123 45610") -> dict:
    """Admin: provision a device, create the customer with it, print the
    welcome letter. Returns what the letter and the label carry."""
    label = admin.form("/admin/devices", {"name": "Living room unit"})
    assert label.status_code == 200 and label.headers["cache-control"] == "no-store"
    identity = parse_identity(label.text)
    created = admin.form("/admin/customers", {"name": name, "username": username,
                                              "email": email, "phone": phone,
                                              "city": "Patiala",
                                              "serial": identity["serial"]})
    assert created.status_code == 303, created.text[:500]
    household_id = created.headers["location"].split("/")[3].split("?")[0]
    admin.get(f"/admin/customers/{household_id}/letter")
    letter = admin.post(f"/admin/customers/{household_id}/letter", {"new_claim_codes": "1"})
    assert letter.status_code == 200 and letter.headers["cache-control"] == "no-store"
    activation = re.search(r'class="code-big">([A-Z]{4}-[A-Z]{4}-[A-Z]{4})<',
                           letter.text).group(1)
    claim = CODE8.findall(letter.text)[0]
    return {"identity": identity, "household_id": household_id, "serial": identity["serial"],
            "device_id": identity["device_id"], "activation": activation, "claim": claim,
            "letter": letter.text, "username": username, "email": email}


STEP1 = {"display_name": "Riya Sharma", "phone": "+91 98123 45610", "email": OWNER_EMAIL,
         "language": "en", "adult": "1"}
STEP2 = {"house": "12-B", "building": "Green Park", "street": "Mall Road",
         "locality": "Sector 4", "landmark": "opposite the SBI ATM", "city": "Patiala",
         "district": "Patiala", "state": "Punjab", "pincode": "147001", "floor": "2",
         "lat": "30.33980", "lon": "76.38690", "entry_note": "Gate 2, guard cabin on the left",
         "police_name": "Civil Lines PS", "police_phone": "0175 2212345"}
STEP3 = {"action": "save", "adults": "2", "children": "1", "seniors": "1",
         "pets": "one dog", "medical": "", "medical_consent": ""}
CONTACT = {"action": "add_contact", "name": "Arjun Sharma", "relation": "brother",
           "phone": "98765 43210", "email": CONTACT_EMAIL, "priority": "1",
           "can_reach_30min": "1"}
STEP6 = {"action": "save", "window": "1800", "safety_defaults": "1", "channel_email": "1",
         "channel_telegram": "1", "channel_sms": "1", "sos_pin": SOS_PIN,
         "sos_pin_confirm": SOS_PIN, "duress_pin": DURESS_PIN,
         "duress_pin_confirm": DURESS_PIN}
CONSENTS = {"consent_account_sos": "1", "consent_sos_snapshot": "1",
            "consent_remote_view": "1", "consent_desk_live_view": "1"}


def links_in(world: World, address: str, path: str) -> list[str]:
    return [m for r, msg in world.mail.sent if r.address == address
            for m in re.findall(rf"https?://\S+?{re.escape(path)}([A-Za-z0-9_\-]+)", msg.text)]


def queued_links(world: World, address: str, path: str) -> list[str]:
    """Links in queued Portal e-mails (before or without the worker)."""
    from sentrix_portal import security
    out = []
    for row in world.notifications():
        if row.channel != "email":
            continue
        try:
            to = security.unseal(row.address_enc)
        except Exception:
            continue
        if to == address:
            out += re.findall(rf"https?://\S+?{re.escape(path)}([A-Za-z0-9_\-]+)", row.body or "")
    return out


def activate(world: World, facts: dict, password: str = PASSWORD) -> PortalBrowser:
    owner = world.browser(ip="203.0.113.10")
    owner.get(f"/activate?u={facts['username']}")
    done = owner.post("/activate", {"username": facts["username"],
                                    "code": facts["activation"], "password": password,
                                    "password_confirm": password})
    assert done.status_code == 303 and done.headers["location"] == "/onboarding", done.text[:300]
    return owner


def link_owner_telegram(world: World, owner: PortalBrowser, chat_id: int = OWNER_CHAT) -> None:
    page = owner.post("/onboarding/6", {"action": "telegram"})
    assert page.status_code == 200, page.text[:300]
    code = re.search(r"/start ([A-Za-z0-9_\-]+)", page.text).group(1)
    telegram_update(world, {"update_id": int(time.time() * 1000) % 10**9 + chat_id % 1000,
                            "message": {"message_id": 1, "text": f"/start {code}",
                                        "chat": {"id": chat_id, "type": "private"},
                                        "from": {"id": chat_id, "first_name": "Riya"}}})


class _NoTelegramApi:
    """Inbound handling never needs the Bot API here; refuse loudly if it does."""

    def __init__(self) -> None:
        self.toasts: list[str] = []

    async def answer_callback(self, callback_id, text):
        self.toasts.append(text)

    async def leave_chat(self, *args, **kwargs):
        raise AssertionError("the bot tried to leave a chat")

    async def aclose(self):
        return None


def telegram_update(world: World, update: dict) -> dict:
    from sentrix_portal.services import notify
    return world.loop.run(notify.handle_update(world.app, update, api=_NoTelegramApi()))


_UPDATES = iter(range(10**6, 10**7))


def telegram_press(world: World, button, chat_id: int = OWNER_CHAT) -> dict:
    """The owner taps an inline button in their private chat."""
    return telegram_update(world, {"update_id": next(_UPDATES), "callback_query": {
        "id": f"cb{next(_UPDATES)}", "data": button.callback_data,
        "from": {"id": chat_id, "first_name": "Riya"},
        "message": {"message_id": 5, "chat": {"id": chat_id, "type": "private"}}}})


def confirm_contact(world: World, address: str = CONTACT_EMAIL) -> None:
    token = queued_links(world, address, "/consent/")[-1]
    contact = world.browser(ip="203.0.113.50")
    page = contact.get(f"/consent/{token}")
    assert page.status_code == 200, page.text[:300]
    done = contact.post(f"/consent/{token}", {"decision": "confirm"})
    assert done.status_code == 200, done.text[:300]


def onboard(world: World, owner: PortalBrowser, facts: dict, *,
            check: Any = None) -> None:
    """The seven wizard steps, the contact's confirmation and the device claim.
    ``check(step, world)`` runs after each step (the scenario test asserts)."""
    from sentrix_portal.services import hub
    check = check or (lambda step, w: None)
    owner.get("/onboarding/1")
    assert owner.post("/onboarding/1", STEP1).headers["location"] == "/onboarding/2"
    check(1, world)
    owner.get("/onboarding/2")
    assert owner.post("/onboarding/2", STEP2).headers["location"] == "/onboarding/3"
    check(2, world)
    owner.get("/onboarding/3")
    step3 = owner.post("/onboarding/3", STEP3)
    assert step3.headers.get("location") == "/onboarding/4", step3.text[:800]
    check(3, world)
    owner.get("/onboarding/4")
    added = owner.post("/onboarding/4", CONTACT)
    assert added.status_code == 200 and "/consent/" in added.text, added.text[:500]
    confirm_contact(world)
    assert owner.post("/onboarding/4", {"action": "continue"}).headers["location"] == \
        "/onboarding/5"
    check(4, world)
    owner.get("/onboarding/5")
    claimed = owner.post("/onboarding/5", {"action": "claim", "serial": facts["serial"],
                                           "claim_code": facts["claim"]})
    assert claimed.status_code == 200 and "is now yours" in claimed.text, claimed.text[:500]
    wait_for(lambda: hub.is_online(facts["device_id"]), timeout=15,
             what="the claimed device online on its token")
    view = owner.get(f"/d/{facts['device_id']}/live")
    assert view.status_code == 200, view.text[:300]
    done5 = owner.post("/onboarding/5", {"action": "device", "device_id": facts["device_id"],
                                         "name": "Living room unit",
                                         "room_label": "Living room", "floor": "Ground",
                                         "mode": "home", "view_checked": "1"})
    assert done5.headers.get("location") == "/onboarding/6", done5.text[:500]
    check(5, world)
    owner.get("/onboarding/6")
    link_owner_telegram(world, owner)
    owner.get("/onboarding/6")
    done6 = owner.post("/onboarding/6", STEP6)
    assert done6.headers.get("location") == "/onboarding/7", done6.text[:800]
    check(6, world)
    review = owner.get("/onboarding/7")
    assert review.status_code == 200
    finish = owner.post("/onboarding/7", CONSENTS)
    assert finish.headers.get("location") == "/home?welcome=1", finish.text[:500]
    check(7, world)


def profile_ready(world: World) -> dict:
    """Wait until the device holds the household's latest profile."""
    from sentrix.link import profile
    from sentrix_portal.models import Household
    with world.db() as db:
        version = db.get(Household, world.facts["household_id"]).profile_version
    wait_for(lambda: profile.current_version() >= version, timeout=15,
             what=f"profile v{version} on the device")
    return profile.current()


def ready_world(world: World) -> PortalBrowser:
    """Provisioned, activated, onboarded, claimed and online. Returns the
    owner's browser; ``world.facts`` holds ids and codes."""
    admin = staff(world, "admin1", "company_admin")
    facts = provision(world, admin)
    world.facts.update(facts)
    world.facts["admin"] = admin
    world.install_device(facts["identity"])
    owner = activate(world, facts)
    onboard(world, owner, facts)
    profile_ready(world)
    world.pump_portal()
    world.tg.sent.clear()
    world.mail.sent.clear()
    world.facts["owner"] = owner
    return owner


def dom_ids(html: str) -> set[str]:
    return set(DOM_ID.findall(html))


__all__ = [name for name in dir() if not name.startswith("_")]
