"""Shared helpers for the T-ACCT tests (accounts, admin, onboarding, household,
settings, privacy). Imported by ``tests/test_acct_*.py``; holds no tests.

Every Portal here runs on its own temporary database and keys
(``PortalSettings.for_tests``). Nothing is sent: ``services.notify.enqueue`` is
replaced by a recorder, and the link hub by a fake that records pushes,
token issues, unpairs and commands (no device, no socket).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct horse battery"
CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')


@dataclass
class Outbox:
    """Recorder for ``services.notify.enqueue``."""

    sent: list[tuple[Any, Any]] = field(default_factory=list)

    def enqueue(self, db, recipient, message):
        self.sent.append((recipient, message))
        return f"n{len(self.sent)}"

    def to(self, address: str) -> list[Any]:
        return [m for r, m in self.sent if r.address == address]

    def codes(self, address: str) -> list[str]:
        """Activation codes in the e-mails to ``address``."""
        return [match for m in self.to(address)
                for match in re.findall(r"Code: ([A-Z-]{14})", m.text)]

    def links(self, address: str, path: str) -> list[str]:
        return [match for m in self.to(address)
                for match in re.findall(rf"https?://\S+{re.escape(path)}(\S+)", m.text)]


@dataclass
class FakeHub:
    """Records every ``services.hub`` call; devices in ``online`` are online."""

    online: set[str] = field(default_factory=set)
    pushes: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    unpaired: list[tuple[str, dict]] = field(default_factory=list)
    commands: list[tuple[str, str, dict, dict]] = field(default_factory=list)
    profiles: list[dict] = field(default_factory=list)

    def install(self, monkeypatch) -> None:
        from sentrix_portal.models import Device
        from sentrix_portal.services import accounts, hub

        async def push_profile(db, device_id):
            device = db.get(Device, device_id)
            self.profiles.append(accounts.build_profile(db, device.household_id, device_id))
            self.pushes.append(device_id)
            return self.profiles[-1]["version"]

        async def issue_token(db, device_id):
            self.tokens.append(device_id)

        async def unpair(db, device_id, actor):
            self.unpaired.append((device_id, actor))
            return {"ok": True, "code": "ok"}

        async def send_command(device_id, name, args, actor, *, timeout=10.0,
                               queue_if_offline=True):
            self.commands.append((device_id, name, args, actor))
            return {"ok": True, "code": "ok", "status": 200, "message": "", "result": None,
                    "queued": False, "command_id": "c1"}

        monkeypatch.setattr(hub, "is_online", lambda device_id: device_id in self.online)
        monkeypatch.setattr(hub, "push_profile", push_profile)
        monkeypatch.setattr(hub, "issue_token", issue_token)
        monkeypatch.setattr(hub, "unpair", unpair)
        monkeypatch.setattr(hub, "send_command", send_command)


class Browser:
    """A TestClient that sends the right CSRF token with every form post."""

    def __init__(self, app, *, ip: str = "testclient"):
        self.app = app
        # A browser asks for HTML: logged-out pages then redirect to /login.
        self.client = TestClient(app, follow_redirects=False, client=(ip, 50000),
                                 headers={"accept": "text/html,application/xhtml+xml"})
        self.csrf: str | None = None
        self.last = None

    def get(self, path: str, **kwargs):
        response = self.client.get(path, **kwargs)
        self._remember(response)
        self.last = response
        return response

    def _remember(self, response) -> None:
        text = response.text if "text/html" in response.headers.get("content-type", "") else ""
        found = CSRF_META.search(text) or CSRF_INPUT.search(text)
        if found:
            self.csrf = found.group(1)

    def post(self, path: str, data: dict | None = None, *, csrf: bool = True, **kwargs):
        body = dict(data or {})
        if csrf and self.csrf and "csrf_token" not in body:
            body["csrf_token"] = self.csrf
        response = self.client.post(path, data=body, **kwargs)
        self._remember(response)
        self.last = response
        return response

    def form(self, path: str, data: dict | None = None, **kwargs):
        """GET the page (for its token), then POST to it."""
        self.get(path)
        return self.post(path, data, **kwargs)

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


@pytest.fixture
def outbox(monkeypatch) -> Outbox:
    from sentrix_portal.services import notify
    box = Outbox()
    monkeypatch.setattr(notify, "enqueue", box.enqueue)
    return box


@pytest.fixture
def fake_hub(monkeypatch) -> FakeHub:
    hub = FakeHub()
    hub.install(monkeypatch)
    return hub


@pytest.fixture
def portal(portal_app, outbox, fake_hub):
    """(app, outbox, hub) for a fresh Portal."""
    from sentrix_portal import security
    security._totp_last_step.clear()
    return portal_app


def add_user(app, *, username: str, role: str, household_id: str | None = None,
             password: str | None = PASSWORD, status: str = "active",
             email: str | None = None, totp_secret: str | None = None,
             phone: str | None = None, name: str | None = None):
    from sentrix_portal import security
    from sentrix_portal.models import User
    with app.state.db.session() as db:
        user = User(username=username, role=role, household_id=household_id,
                    display_name=name or username.title(), status=status, email=email,
                    password_hash=security.hash_password(password) if password else None,
                    phone_enc=security.seal(phone) if phone else None)
        if totp_secret:
            user.totp_secret_enc = security.seal(totp_secret)
            user.totp_enabled = True
            user.recovery_codes = []
        db.add(user)
        db.flush()
        return user.id


def add_household(app, *, code: str = "SX-0001", name: str = "Riya Sharma (demo)",
                  complete: bool = False, owner: str = "riya",
                  phone: str = "+919812345610", email: str = "riya@example.com") -> dict:
    """A household with an active owner (password set); ``complete`` fills
    every onboarding step's data and a claimed device."""
    from sentrix_portal import security
    from sentrix_portal.models import (Camera, Consent, Device, EmergencyContact,
                                       Household, utcnow)
    with app.state.db.session() as db:
        home = Household(customer_code=code, display_name=name, status="active",
                         channels={"email": True, "telegram": False, "sms": True},
                         occupants={}, nearest={})
        db.add(home)
        db.flush()
        result = {"household_id": home.id}
    result["owner_id"] = add_user(app, username=owner, role="owner",
                                  household_id=result["household_id"], email=email,
                                  phone=phone, name=name)
    if complete:
        with app.state.db.session() as db:
            home = db.get(Household, result["household_id"])
            home.house, home.street, home.locality = "12", "Mall Road", "Sector 4"
            home.landmark, home.city, home.state = "near SBI ATM", "Patiala", "Punjab"
            home.pincode, home.lat, home.lon = "147001", 30.3398, 76.3869
            home.occupants = {"adults": 2, "children": 1, "seniors": 1}
            home.sos_pin_hash = security.hash_pin("482916")
            home.onboarding_step = 7
            home.onboarding_completed_at = utcnow()
            device = Device(serial=f"SX5-{code[-4:]}01", household_id=home.id,
                            state="CLAIMED", name="Living room unit", claimed_at=utcnow())
            db.add(device)
            db.flush()
            db.add(Camera(device_id=device.id, camera_id="CAM_0", room_label="Living room",
                          floor="Ground"))
            contact = EmergencyContact(household_id=home.id, name="Arjun", relation="brother",
                                       phone_enc=security.seal("+919876543210"),
                                       email="arjun@example.com", priority=1,
                                       consent_status="CONFIRMED", confirmed_at=utcnow())
            db.add(contact)
            for purpose in ("account_sos", "sos_snapshot", "remote_view"):
                db.add(Consent(household_id=home.id, subject_type="user",
                               subject_id=result["owner_id"], purpose=purpose,
                               notice_version="2026-10-1", granted_at=utcnow(),
                               method="wizard"))
            db.flush()
            result.update(device_id=device.id, contact_id=contact.id, serial=device.serial)
    return result


def add_device(app, *, serial: str, household_id: str | None = None,
               state: str = "UNASSIGNED", claim_code: str = "KTRW-BQXL") -> str:
    from sentrix_portal import security
    from sentrix_portal.models import Device
    with app.state.db.session() as db:
        device = Device(serial=serial, household_id=household_id, state=state,
                        claim_code_hash=security.code_digest(claim_code))
        db.add(device)
        db.flush()
        return device.id


def staff_browser(app, role: str = "company_admin", username: str = "admin1") -> Browser:
    import pyotp
    secret = pyotp.random_base32()
    add_user(app, username=username, role=role, totp_secret=secret)
    browser = Browser(app)
    response = browser.login_staff(username, secret)
    assert response.status_code == 303, response.text
    return browser


def owner_browser(app, username: str = "riya", **kwargs) -> Browser:
    browser = Browser(app, **kwargs)
    response = browser.login(username)
    assert response.status_code == 303, response.text
    return browser


def query(app, model, **filters):
    with app.state.db.session() as db:
        rows = db.query(model).filter_by(**filters).all()
        db.expunge_all()
        return rows


__all__ = ["PASSWORD", "Outbox", "FakeHub", "Browser", "outbox", "fake_hub", "portal",
           "add_user", "add_household", "add_device", "staff_browser", "owner_browser",
           "query"]
