"""The company admin console and the provisioning tool: customers, welcome
letter, devices (provision, label, assign, transfer, retire, fleet), staff and
the audit log; ``tools/provision_device.py`` (docs/7 §5.2, §10, D14)."""

from __future__ import annotations

import hashlib
import json
import re
import sys

import pytest

from sentrix_portal import security
from sentrix_portal.models import (ActivationCode, AuditLog, Camera, Device, Household,
                                   User, UserSession)
from test_acct_support import (PASSWORD, add_device, add_household,  # noqa: F401
                               add_user, fake_hub, outbox, portal, query)
from test_portal_api_v2_support import Api, api_as, staff_api

#: A claim / setup code (8 letters).
CODE8 = re.compile(r"[B-DF-HJ-NP-TV-XZ]{4}-[B-DF-HJ-NP-TV-XZ]{4}")


def _create(admin: Api, **overrides):
    data = {"name": "Riya Sharma", "username": "riya", "email": "riya@example.com",
            "phone": "98123 45610", "city": "Patiala", **overrides}
    return admin.post("/admin/customers", data)


class TestCustomers:
    def test_create_customer_makes_a_pending_owner_without_password(self, portal):
        admin = staff_api(portal)
        assert _create(admin).status_code == 201
        owner = query(portal, User, username="riya")[0]
        assert owner.role == "owner" and owner.status == "pending_activation"
        assert owner.password_hash is None
        assert security.unseal(owner.phone_enc) == "+919812345610"
        household = query(portal, Household, id=owner.household_id)[0]
        assert household.customer_code == "SX-0001" and household.safety_defaults is True
        assert query(portal, AuditLog, event="admin.customer_created")

    def test_validation_and_unique_username(self, portal):
        admin = staff_api(portal)
        assert _create(admin, username="Bad Name!").status_code == 422
        assert _create(admin, email="not-an-email").status_code == 422
        assert _create(admin, phone="12345").status_code == 422
        assert _create(admin).status_code == 201
        taken = _create(admin, email="other@example.com")
        assert taken.status_code == 422 and "taken" in taken.json()["error"]["fields"]["username"]

    def test_create_with_a_device_and_print_the_letter(self, portal):
        admin = staff_api(portal)
        add_device(portal, serial="SX5-000123", claim_code="KTRW-BQXL")
        household_id = _create(admin, serial="SX5-000123").json()["id"]
        device = query(portal, Device, serial="SX5-000123")[0]
        assert device.state == "ASSIGNED" and device.household_id == household_id
        letter = admin.post(f"/admin/customers/{household_id}/letter",
                            {"new_claim_codes": True}).json()
        assert letter["username"] == "riya"
        assert letter["portal_url"] == "http://portal.localhost:8100"
        (row,) = letter["devices"]
        assert row["serial"] == "SX5-000123"
        claim = row["claim_code"]
        assert CODE8.fullmatch(claim) and claim != "KTRW-BQXL", "a fresh claim code"
        assert row["qr_url"] == row["claim_url"] + f"#code={claim}"
        device = query(portal, Device, serial="SX5-000123")[0]
        assert device.claim_code_hash == security.code_digest(claim)

    def test_letter_can_keep_the_label_claim_code(self, portal):
        admin = staff_api(portal)
        add_device(portal, serial="SX5-000124", claim_code="KTRW-BQXL")
        household_id = _create(admin, serial="SX5-000124").json()["id"]
        letter = admin.post(f"/admin/customers/{household_id}/letter", {}).json()
        assert letter["devices"][0]["claim_code"] is None, "the label code stays"
        assert query(portal, Device, serial="SX5-000124")[0].claim_code_hash == \
            security.code_digest("KTRW-BQXL")

    def test_deactivate_ends_sessions_and_blocks_sign_in(self, portal):
        home = add_household(portal, complete=True)
        owner = api_as(portal, home["owner_id"])
        admin = staff_api(portal)
        admin.post(f"/admin/customers/{home['household_id']}/status",
                   {"action": "deactivate"})
        assert owner.get("/home").status_code == 401
        assert Api(portal).login("riya").status_code == 403
        admin.post(f"/admin/customers/{home['household_id']}/status",
                   {"action": "reactivate"})
        assert Api(portal).login("riya").status_code == 200

    def test_resend_without_email_is_refused(self, portal, outbox):
        admin = staff_api(portal)
        home = add_household(portal, email=None)
        response = admin.post(f"/admin/customers/{home['household_id']}/resend")
        assert response.status_code == 409 and not outbox.sent

    def test_owners_and_operators_cannot_use_the_console(self, portal):
        home = add_household(portal, complete=True)
        owner = api_as(portal, home["owner_id"])
        desk = staff_api(portal, role="operator", username="desk1")
        for api in (owner, desk):
            assert api.get("/admin/overview").status_code == 403
            assert api.get(f"/admin/customers/{home['household_id']}").status_code == 403
            assert api.post("/admin/customers", {"name": "x"}).status_code == 403


class TestDevices:
    def test_provision_shows_secrets_once_and_stores_digests(self, portal):
        admin = staff_api(portal)
        response = admin.post("/admin/devices", {"name": "Bench unit"})
        assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
        label = response.json()
        identity = label["identity"]
        device = query(portal, Device, id=identity["device_id"])[0]
        assert device.serial == identity["serial"] == "SX5-000001"
        assert device.state == "UNASSIGNED"
        assert device.bootstrap_hash == security.token_digest(identity["bootstrap_secret"])
        assert device.claim_code_hash == security.code_digest(label["claim_code"])
        assert identity["setup_code_digest"] == hashlib.sha256(
            label["setup_code"].replace("-", "").encode()).hexdigest()
        assert re.fullmatch(r"[0-9a-f-]{36}", identity["device_id"])
        # Nothing to see later: the label needs a reprint (a new claim code).
        page = admin.get(f"/admin/devices/{device.id}/label")
        assert label["claim_code"] not in page.text
        assert identity["bootstrap_secret"] not in admin.get("/admin/devices").text

    def test_serials_are_unique_and_well_formed(self, portal):
        admin = staff_api(portal)
        add_device(portal, serial="SX5-000007")
        assert admin.post("/admin/devices", {"serial": "SX5-000007"}).status_code == 422
        assert admin.post("/admin/devices", {"serial": "ABC"}).status_code == 422
        assert admin.post("/admin/devices", {}).status_code == 201
        assert query(portal, Device, serial="SX5-000008")

    def test_reprint_rotates_the_claim_code(self, portal):
        admin = staff_api(portal)
        device_id = add_device(portal, serial="SX5-000010", claim_code="KTRW-BQXL")
        code = admin.post(f"/admin/devices/{device_id}/label").json()["claim_code"]
        assert code != "KTRW-BQXL"
        assert query(portal, Device, id=device_id)[0].claim_code_hash == \
            security.code_digest(code)

    def test_assign_transfer_and_retire(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        admin = staff_api(portal)
        spare = add_device(portal, serial="SX5-000050")
        assert admin.post(f"/admin/devices/{spare}/assign",
                          {"customer_code": "SX-0001"}).status_code == 200
        assert query(portal, Device, id=spare)[0].state == "ASSIGNED"
        assert admin.post(f"/admin/devices/{spare}/assign",
                          {"customer_code": "SX-0001"}).status_code == 409
        transfer = admin.post(f"/admin/devices/{home['device_id']}/transfer")
        assert transfer.status_code == 200 and transfer.json()["claim_code"]
        assert fake_hub.unpaired and fake_hub.unpaired[0][0] == home["device_id"]
        assert fake_hub.unpaired[0][1]["type"] == "cloud"
        device = query(portal, Device, id=home["device_id"])[0]
        assert device.state == "UNASSIGNED" and device.household_id is None
        assert device.token_hash is None
        assert not query(portal, Camera, device_id=home["device_id"]), "room labels go"
        admin.post(f"/admin/devices/{spare}/retire")
        retired = query(portal, Device, id=spare)[0]
        assert retired.state == "RETIRED" and retired.claim_code_hash is None

    def test_fleet_status_reads_the_hub(self, portal, fake_hub):
        home = add_household(portal, complete=True)
        admin = staff_api(portal)
        (row,) = admin.get("/admin/devices").json()["rows"]
        assert row["online"] is False
        fake_hub.online.add(home["device_id"])
        page = admin.get("/admin/devices", params={"state": "CLAIMED"})
        assert page.json()["rows"][0]["online"] is True
        overview = admin.get("/admin/overview").json()
        assert overview["online"] == 1 and overview["claimed"] == 1
        assert f"/d/{home['device_id']}" not in page.text, "no console link for staff"


class TestStaff:
    def test_create_operator_then_activate_and_set_up_totp(self, portal, outbox):
        import pyotp
        admin = staff_api(portal)
        page = admin.post("/admin/operators", {"role": "operator", "username": "desk1",
                                               "display_name": "Desk One",
                                               "email": "desk1@example.com"})
        assert page.status_code == 201 and page.headers["cache-control"] == "no-store"
        code = page.json()["activation_code"]
        assert outbox.codes("desk1@example.com") == [code]
        desk = Api(portal)
        desk.session()
        activated = desk.post("/auth/activate", {"username": "desk1", "code": code,
                                                 "password": PASSWORD,
                                                 "password_confirm": PASSWORD})
        assert activated.json()["next"] == "/account/2fa"
        desk.csrf = activated.json()["session"]["csrf_token"]
        assert desk.get("/account").status_code == 403
        desk.get("/account/2fa")
        secret = security.unseal(query(portal, User, username="desk1")[0].totp_secret_enc)
        security._totp_last_step.clear()
        assert desk.post("/account/2fa/setup",
                         {"code": pyotp.TOTP(secret).now()}).status_code == 200
        assert desk.get("/account").status_code == 200

    def test_disable_and_reset_totp(self, portal):
        admin = staff_api(portal)
        desk = staff_api(portal, role="operator", username="desk2")
        desk_id = query(portal, User, username="desk2")[0].id
        admin.post(f"/admin/operators/{desk_id}", {"action": "reset_2fa"})
        user = query(portal, User, id=desk_id)[0]
        assert not user.totp_enabled and user.totp_secret_enc is None
        assert desk.get("/account").status_code == 401, "signed out"
        admin.post(f"/admin/operators/{desk_id}", {"action": "disable"})
        assert query(portal, User, id=desk_id)[0].status == "disabled"
        me = query(portal, User, username="admin1")[0].id
        assert admin.post(f"/admin/operators/{me}", {"action": "disable"}).status_code == 409


class TestAudit:
    def test_audit_verifies_the_chain(self, portal):
        admin = staff_api(portal)
        _create(admin)
        page = admin.get("/admin/audit", params={"event": "admin."}).json()
        assert page["chain"]["ok"] is True
        assert "admin.customer_created" in {r["event"] for r in page["rows"]}

    def test_tampering_is_reported(self, portal):
        admin = staff_api(portal)
        _create(admin)
        with portal.state.db.session() as db:
            row = db.query(AuditLog).filter_by(event="admin.customer_created").first()
            row.detail = {"customer_code": "SX-9999"}
        assert admin.get("/admin/audit").json()["chain"]["ok"] is False


# ── tools/provision_device.py ─────────────────────────────────────────────────
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """The CLI on its own Portal database and keys; .env is never read."""
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("PORTAL_DATABASE_URL", f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    monkeypatch.setenv("PORTAL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PORTAL_SECRET_KEY", "s" * 64)
    monkeypatch.setenv("PORTAL_DATA_KEY", "d" * 64)
    monkeypatch.setenv("PORTAL_BASE_URL", "http://portal.localhost:8100")
    from tools import provision_device
    yield provision_device, tmp_path


class TestProvisionTool:
    def test_device_identity_label_and_digests(self, cli_env, capsys):
        from sentrix_portal.db import Database
        tool, tmp_path = cli_env
        out = tmp_path / "card" / "etc" / "sentrix"
        assert tool.main(["--out", str(out), "--label", str(tmp_path / "label.html"),
                          "--name", "Unit A"]) == 0
        printed = capsys.readouterr().out
        identity = json.loads((out / "identity.json").read_text(encoding="utf-8"))
        assert set(identity) == {"device_id", "serial", "bootstrap_secret",
                                 "setup_code_digest", "provisioned_at"}
        if sys.platform != "win32":
            assert (out / "identity.json").stat().st_mode & 0o777 == 0o600
        claim = re.search(r"claim code\s+(\S+)", printed).group(1)
        setup = re.search(r"setup code\s+(\S+)", printed).group(1)
        assert identity["setup_code_digest"] == hashlib.sha256(
            setup.replace("-", "").encode()).hexdigest()
        database = Database(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
        with database.session() as db:
            device = db.get(Device, identity["device_id"])
            assert device.serial == identity["serial"] and device.state == "UNASSIGNED"
            assert device.claim_code_hash == security.code_digest(claim)
            assert device.bootstrap_hash == security.token_digest(identity["bootstrap_secret"])
        database.dispose()
        label = (tmp_path / "label.html").read_text(encoding="utf-8")
        assert claim in label and '<svg class="qr"' in label and "TEST MODE" in label

    def test_refuses_to_overwrite_and_assigns(self, cli_env, capsys):
        tool, tmp_path = cli_env
        out = tmp_path / "id"
        assert tool.main(["device", "--out", str(out)]) == 0
        first = (out / "identity.json").read_text(encoding="utf-8")
        assert tool.main(["--out", str(out)]) == 2
        assert (out / "identity.json").read_text(encoding="utf-8") == first
        assert tool.main(["--out", str(out), "--assign", "SX-0404"]) == 2
        assert tool.main(["--out", str(out), "--force", "--serial", "SX5-000777"]) == 0
        assert json.loads((out / "identity.json").read_text())["serial"] == "SX5-000777"

    def test_first_admin(self, cli_env, capsys):
        from sentrix_portal.db import Database
        tool, tmp_path = cli_env
        assert tool.main(["admin", "--username", "founder", "--email", "f@example.com"]) == 0
        code = re.search(r"activation\s+(\S+)", capsys.readouterr().out).group(1)
        database = Database(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
        with database.session() as db:
            user = db.query(User).filter_by(username="founder").one()
            assert user.role == "company_admin" and user.password_hash is None
            row = db.query(ActivationCode).filter_by(user_id=user.id).one()
            assert row.code_hash == security.code_digest(code)
        database.dispose()
        assert tool.main(["admin", "--username", "founder"]) == 2


def test_sessions_are_server_side(portal):
    add_household(portal)
    assert Api(portal).login("riya").status_code == 200
    rows = query(portal, UserSession)
    assert len(rows) == 1 and rows[0].csrf_token
