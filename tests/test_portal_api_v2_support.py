"""Shared helpers for the Portal API v2 tests (``tests/test_portal_api_v2_*.py``);
holds no tests.

Every Portal runs on its own temporary database and keys
(``PortalSettings.for_tests``); nothing is sent (``notify.enqueue`` is a
recorder) and no device is connected (the link hub is either the recording
fake of ``test_acct_support`` or absent, so SOS answers take the cloud path).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from test_acct_support import PASSWORD

V2 = "/api/v2"


class Api:
    """A browser running the single-page app: JSON in and out, the CSRF
    token of ``GET /session`` sent back as ``X-CSRF-Token``."""

    def __init__(self, app, *, ip: str = "testclient"):
        self.app = app
        self.client = TestClient(app, follow_redirects=False, client=(ip, 50000))
        self.csrf: str | None = None

    # plumbing
    def _headers(self, csrf: bool, headers: dict | None) -> dict:
        out = dict(headers or {})
        if csrf and self.csrf:
            out.setdefault("X-CSRF-Token", self.csrf)
        return out

    def get(self, path: str, **kwargs):
        return self.client.get(V2 + path, **kwargs)

    def post(self, path: str, json: Any = None, *, csrf: bool = True, headers=None):
        return self.client.post(V2 + path, json=json, headers=self._headers(csrf, headers))

    def put(self, path: str, json: Any = None, *, csrf: bool = True, headers=None):
        return self.client.put(V2 + path, json=json, headers=self._headers(csrf, headers))

    def patch(self, path: str, json: Any = None, *, csrf: bool = True, headers=None):
        return self.client.patch(V2 + path, json=json, headers=self._headers(csrf, headers))

    def delete(self, path: str, *, csrf: bool = True, headers=None):
        return self.client.delete(V2 + path, headers=self._headers(csrf, headers))

    # sessions
    def session(self) -> dict:
        data = self.get("/session").json()
        self.csrf = data.get("csrf_token") or data.get("login_csrf_token")
        return data

    def login(self, username: str, password: str = PASSWORD):
        self.session()
        response = self.post("/auth/login", {"username": username, "password": password})
        if response.status_code == 200:
            self.csrf = response.json()["session"]["csrf_token"]
        return response

    def login_staff(self, username: str, secret: str, password: str = PASSWORD):
        import pyotp

        from sentrix_portal import security
        assert self.login(username, password).status_code == 200
        security._totp_last_step.clear()
        return self.post("/account/2fa/verify", {"code": pyotp.TOTP(secret).now()})

    def step_up(self, password: str = PASSWORD):
        response = self.post("/auth/step-up", {"password": password})
        assert response.status_code == 200, response.text
        return response


def api_as(app, user_id: str, *, totp: bool = False, ip: str = "testclient") -> Api:
    """An API client signed in as ``user_id`` (a session made directly)."""
    from test_link_support import login
    raw, csrf = login(app, user_id, totp=totp)
    api = Api(app, ip=ip)
    api.client.cookies.set("portal_session", raw)
    api.csrf = csrf
    return api


def staff_api(app, role: str = "company_admin", username: str = "admin1") -> Api:
    import pyotp

    from test_acct_support import add_user
    secret = pyotp.random_base32()
    add_user(app, username=username, role=role, totp_secret=secret)
    api = Api(app)
    response = api.login_staff(username, secret)
    assert response.status_code == 200, response.text
    return api


def expire_step_up(app) -> None:
    """Every session's password confirmation is an hour old (a sign-in
    counts as one; the step-up rules need it gone)."""
    from datetime import timedelta

    from sentrix_portal.models import UserSession, utcnow
    with app.state.db.session() as db:
        for row in db.query(UserSession):
            row.step_up_at = utcnow() - timedelta(hours=1)


def error_of(response) -> dict:
    """The docs/9 §4 error object (asserting the shape)."""
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert isinstance(error["code"], str) and isinstance(error["message"], str)
    assert error["message"] and "Traceback" not in error["message"]
    return error


def secrets_in(app) -> list[str]:
    """Every stored secret, hash or digest that must never reach a response."""
    from sentrix_portal.models import (ActionToken, ActivationCode, AuditLog, Device,
                                       Household, User, UserSession)
    from sentrix_portal.settings import current
    values: list[str] = []
    with app.state.db.session() as db:
        for user in db.query(User):
            values += [user.password_hash, user.totp_secret_enc, user.phone_enc,
                       user.last_login_ip_hash, *(user.recovery_codes or [])]
        for home in db.query(Household):
            values += [home.sos_pin_hash, home.duress_pin_hash, home.medical_enc]
        for row in db.query(UserSession):
            values += [row.id_hash, row.ip_hash]
        for row in db.query(ActionToken):
            values += [row.token_hash]
        for row in db.query(ActivationCode):
            values += [row.code_hash]
        for device in db.query(Device):
            values += [device.claim_code_hash, device.bootstrap_hash, device.token_hash]
        for row in db.query(AuditLog):
            values += [row.hash, row.prev_hash, row.ip_hash]
    settings = current()
    values += [settings.secret_key, settings.data_key]
    return [v for v in values if isinstance(v, str) and len(v) >= 16 and set(v) != {"0"}]


def assert_no_secrets(app, *responses) -> None:
    found = secrets_in(app)
    for response in responses:
        text = response.text
        for value in found:
            assert value not in text, (response.request.url.path, value[:12])
        lowered = text.lower()
        assert "$argon2" not in lowered, response.request.url.path
        for key in ('"password_hash"', '"pin_hash"', '"token_hash"', '"totp_secret',
                    '"recovery_codes":["', '"id_hash"', '"code_hash"'):
            assert key not in lowered, (response.request.url.path, key)


@pytest.fixture
def sos_world(tmp_path, monkeypatch):
    """A seeded household (owner, member, operator, confirmed contact,
    claimed device, PIN and duress PIN), another household, test mode with a
    simulated Desk number; no device connected (the cloud path)."""
    from sentrix_portal.app import create_app
    from sentrix_portal.services import hub, notify
    from sentrix_portal.services import sos as portal_sos
    from sentrix_portal.settings import PortalSettings
    from test_link_support import DESK_PHONE, Outbox, seed_portal
    hub.reset()
    portal_sos.reset_pin_limiter()
    app = create_app(PortalSettings.for_tests(tmp_path, desk_number=DESK_PHONE))
    seeded = seed_portal(app, other_household=True)
    outbox = Outbox()
    monkeypatch.setattr(notify, "enqueue", outbox)
    yield app, seeded, outbox
    app.state.db.dispose()
    hub.reset()


def open_request(app, seeded, *, incident_type: str = "weapon", level: int = 3,
                 approved: bool = False, snapshot: bool = False) -> str:
    """A request as the cloud opens it while the device is offline."""
    from sentrix.sos import states as S
    from sentrix_portal.models import Device, IncidentMirror, SosSnapshot, utcnow
    from sentrix_portal.services import sos as portal_sos
    with app.state.db.session() as db:
        device = db.get(Device, seeded.device_id)
        incident = str(uuid.uuid4())
        db.add(IncidentMirror(id=incident, device_id=device.id,
                              household_id=device.household_id, started_at=utcnow(),
                              peak_level=level, incident_type=incident_type,
                              reason="handgun"))
        row = portal_sos.create_cloud_request(db, device, seeded.household_id,
                                              incident_type=incident_type, level=level,
                                              incident_ids=[incident])
        portal_sos._cloud_event(db, row, S.PROMPTED)
        if approved:
            portal_sos._cloud_event(db, row, S.APPROVE, actor_type="owner",
                                    actor_id=f"portal:{seeded.owner_id}", channel="portal",
                                    authority="POLICE")
            portal_sos._cloud_event(db, row, S.DELIVERED, detail={"target": "desk"})
        if snapshot:
            db.add(SosSnapshot(request_id=row.id, device_id=device.id,
                               data=b"\xff\xd8jpeg", sha256="x", expires_at=utcnow()))
        return row.id


def mint(app, *, purpose: str, request_id: str, subject_type: str, subject_id: str) -> str:
    """A raw action-link / contact-page token for a request."""
    from sentrix_portal.models import AssistanceRequest
    from sentrix_portal.services import sos as portal_sos
    with app.state.db.session() as db:
        row = db.get(AssistanceRequest, request_id)
        raw, _ = portal_sos.mint_token(db, purpose=purpose, row=row,
                                       subject_type=subject_type, subject_id=subject_id)
        return raw


__all__ = ["V2", "Api", "api_as", "staff_api", "expire_step_up", "error_of", "secrets_in",
           "assert_no_secrets", "sos_world", "open_request", "mint"]
