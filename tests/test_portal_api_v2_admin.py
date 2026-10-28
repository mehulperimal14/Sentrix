"""Portal API v2: the company admin console (company_admin + TOTP):
overview, customers, the welcome letter as data, the device fleet and labels
as data, staff accounts and the audit chain; who may not reach it; and the
OpenAPI document the app's types are generated from."""

from __future__ import annotations

from sentrix_portal.models import AuditLog, Device, Household, User
from test_acct_support import (add_device, add_household, add_user, fake_hub,  # noqa: F401
                               outbox, portal, query)
from test_portal_api_v2_support import (Api, api_as, assert_no_secrets, error_of,
                                        staff_api)


def test_who_may_use_the_admin_console(portal):
    home = add_household(portal)
    owner = api_as(portal, home["owner_id"])
    assert error_of(owner.get("/admin/overview"))["code"] == "forbidden"
    desk = staff_api(portal, role="operator", username="desk1")
    assert error_of(desk.get("/admin/customers"))["code"] == "forbidden"
    no_totp = api_as(portal, add_user(portal, username="admin2", role="company_admin"))
    assert error_of(no_totp.get("/admin/overview"))["code"] == "totp_required"
    assert error_of(Api(portal).get("/admin/overview"))["code"] == "not_signed_in"


def test_overview_and_customers(portal, outbox):
    admin = staff_api(portal)
    add_device(portal, serial="SX5-000001")
    bad = admin.post("/admin/customers", {"name": "A", "username": "Bad Name",
                                          "email": "nope", "serial": "SX5-999999"})
    assert bad.status_code == 422
    fields = error_of(bad)["fields"]
    assert fields["name"] == "Customer name must be 2-80 characters."
    assert fields["email"] == "E-mail: enter a valid e-mail address."
    assert fields["serial"] == "Choose an unassigned device (or none)."
    assert fields["username"].startswith("Username: 3-32 characters")
    created = admin.post("/admin/customers", {"name": "Asha Rao", "username": "asha",
                                              "email": "asha@example.com",
                                              "phone": "+919812300000", "city": "Patiala",
                                              "serial": "SX5-000001"})
    assert created.status_code == 201, created.text
    customer = created.json()
    assert customer["message"] == "Customer created. Print the welcome letter next."
    assert customer["owner"]["status"] == "pending_activation"
    assert customer["owner"]["email_masked"] != "asha@example.com"
    assert customer["devices"][0]["state"] == "ASSIGNED"
    taken = admin.post("/admin/customers", {"name": "Asha 2", "username": "asha",
                                            "email": "a2@example.com"})
    assert error_of(taken)["fields"] == {"username": "That username is taken."}
    listing = admin.get("/admin/customers", params={"q": "patiala"}).json()
    assert [r["customer_code"] for r in listing["rows"]] == [customer["customer_code"]]
    overview = admin.get("/admin/overview").json()
    assert overview["customers"] == 1 and overview["pending"] == 1
    assert overview["chain"]["ok"] is True
    detail = admin.get(f"/admin/customers/{customer['id']}")
    assert detail.json()["events"][0]["at"].endswith("Z")
    assert_no_secrets(portal, created, detail)


def test_welcome_letter_shows_the_code_once(portal, outbox):
    admin = staff_api(portal)
    created = admin.post("/admin/customers", {"name": "Asha Rao", "username": "asha",
                                              "email": "asha@example.com"}).json()
    add_device(portal, serial="SX5-000002", household_id=created["id"], state="ASSIGNED")
    state = admin.get(f"/admin/customers/{created['id']}/letter").json()
    assert state["printable"] is True and state["devices"][0]["serial"] == "SX5-000002"
    letter = admin.post(f"/admin/customers/{created['id']}/letter",
                        {"new_claim_codes": True})
    assert letter.status_code == 200, letter.text
    assert letter.headers["cache-control"] == "no-store"
    data = letter.json()
    assert data["username"] == "asha" and len(data["activation_code"]) == 14
    assert data["activation_url"].endswith(data["activation_code"].replace("-", ""))
    (device,) = data["devices"]
    assert device["claim_code"] and device["qr_url"].endswith(f"#code={device['claim_code']}")
    assert any(r.event == "admin.letter_printed" for r in query(portal, AuditLog))
    again = admin.get(f"/admin/customers/{created['id']}")
    assert data["activation_code"] not in again.text and device["claim_code"] not in again.text
    # Activated: no more letters, only a reset e-mail.
    with portal.state.db.session() as db:
        db.query(User).filter_by(username="asha").one().status = "active"
    refused = admin.post(f"/admin/customers/{created['id']}/letter", {})
    assert refused.status_code == 409
    assert "chose a password" in error_of(refused)["message"]
    reset = admin.post(f"/admin/customers/{created['id']}/resend")
    assert reset.json()["message"] == "E-mail queued."
    assert outbox.codes("asha@example.com"), "the code goes to the customer only"
    assert not any(c in reset.text for c in outbox.codes("asha@example.com"))


def test_customer_status_and_assign(portal):
    admin = staff_api(portal)
    created = admin.post("/admin/customers", {"name": "Asha Rao", "username": "asha",
                                              "email": "asha@example.com"}).json()
    off = admin.post(f"/admin/customers/{created['id']}/status", {"action": "deactivate"})
    assert off.json()["status"] == "disabled" and off.json()["message"] == \
        "Customer deactivated."
    add_device(portal, serial="SX5-000003")
    refused = admin.post(f"/admin/customers/{created['id']}/assign", {"serial": "SX5-000003"})
    assert refused.status_code == 409
    assert error_of(refused)["message"] == "the customer account is not active"
    on = admin.post(f"/admin/customers/{created['id']}/status", {"action": "reactivate"})
    assert on.json()["status"] == "active"
    assigned = admin.post(f"/admin/customers/{created['id']}/assign", {"serial": "sx5 000003"})
    assert assigned.json()["devices"][0]["serial"] == "SX5-000003"
    assert admin.post(f"/admin/customers/{created['id']}/assign",
                      {"serial": "SX5-000404"}).status_code == 404
    assert admin.get("/admin/customers/nope").status_code == 404


def test_devices_provision_label_assign_transfer_retire(portal, fake_hub):
    admin = staff_api(portal)
    provisioned = admin.post("/admin/devices", {"name": "Demo unit"})
    assert provisioned.status_code == 201, provisioned.text
    label = provisioned.json()
    assert label["serial"] == "SX5-000001" and label["claim_code"] and label["setup_code"]
    assert label["identity"]["device_id"] == label["device_id"]
    assert label["identity"]["bootstrap_secret"], "identity.json is shown once, here"
    assert label["qr_url"].endswith(f"#code={label['claim_code']}")
    assert provisioned.headers["cache-control"] == "no-store"
    duplicate = admin.post("/admin/devices", {"serial": "SX5-000001"})
    assert error_of(duplicate)["fields"] == {"serial": "serial SX5-000001 is already "
                                                       "provisioned"}
    fleet = admin.get("/admin/devices")
    assert fleet.json()["next_serial"] == "SX5-000002"
    assert label["identity"]["bootstrap_secret"] not in fleet.text
    assert_no_secrets(portal, fleet)
    device_id = label["device_id"]
    assert admin.get(f"/admin/devices/{device_id}/label").json()["reprintable"] is True
    reprint = admin.post(f"/admin/devices/{device_id}/label").json()
    assert reprint["claim_code"] != label["claim_code"] and reprint["identity"] is None
    home = add_household(portal)
    unknown = admin.post(f"/admin/devices/{device_id}/assign", {"customer_code": "SX-9999"})
    assert unknown.status_code == 404
    assert error_of(unknown)["message"] == "Check the customer code (SX-0001)."
    assigned = admin.post(f"/admin/devices/{device_id}/assign", {"customer_code": "sx-0001"})
    assert assigned.json()["state"] == "ASSIGNED" and assigned.json()["customer_code"] == \
        "SX-0001"
    with portal.state.db.session() as db:
        db.get(Device, device_id).state = "CLAIMED"
    moved = admin.post(f"/admin/devices/{device_id}/transfer")
    assert moved.status_code == 200 and moved.json()["title"] == "Device transferred"
    assert fake_hub.unpaired and fake_hub.unpaired[0][0] == device_id
    assert query(portal, Device, id=device_id)[0].household_id is None
    retired = admin.post(f"/admin/devices/{device_id}/retire")
    assert retired.json()["message"] == "Device retired."
    again = admin.post(f"/admin/devices/{device_id}/transfer")
    assert again.status_code == 409 and error_of(again)["code"] == "conflict"
    assert query(portal, Household, id=home["household_id"])


def test_staff_accounts(portal, outbox):
    admin = staff_api(portal)
    bad = admin.post("/admin/operators", {"username": "x", "display_name": ""})
    assert set(error_of(bad)["fields"]) == {"username", "display_name"}
    created = admin.post("/admin/operators", {"role": "operator", "username": "desk5",
                                              "display_name": "Kartik",
                                              "email": "kartik@example.com"})
    assert created.status_code == 201, created.text
    data = created.json()
    assert len(data["activation_code"]) == 14 and data["user"]["status"] == \
        "pending_activation"
    assert created.headers["cache-control"] == "no-store"
    listing = admin.get("/admin/operators")
    assert {s["username"] for s in listing.json()["staff"]} == {"admin1", "desk5"}
    assert data["activation_code"] not in listing.text
    me = next(s for s in listing.json()["staff"] if s["me"])
    assert error_of(admin.post(f"/admin/operators/{me['id']}", {"action": "disable"}))[
        "code"] == "conflict"
    disabled = admin.post(f"/admin/operators/{data['user']['id']}", {"action": "disable"})
    assert disabled.json()["status"] == "disabled"
    impossible = admin.post(f"/admin/operators/{data['user']['id']}", {"action": "resend"})
    assert impossible.status_code == 409
    assert any(r.event == "admin.staff_disable" for r in query(portal, AuditLog))


def test_audit_chain_with_a_cursor(portal):
    admin = staff_api(portal)
    for n in range(4):
        admin.post("/admin/devices", {"name": f"unit {n}"})
    first = admin.get("/admin/audit", params={"limit": 2})
    data = first.json()
    assert data["chain"]["ok"] is True and len(data["rows"]) == 2 and data["next_cursor"]
    older = admin.get("/admin/audit", params={"limit": 2, "cursor": data["next_cursor"]})
    assert older.json()["rows"][0]["id"] < data["rows"][-1]["id"]
    filtered = admin.get("/admin/audit", params={"event": "admin.device"}).json()
    assert {r["event"] for r in filtered["rows"]} == {"admin.device_provisioned"}
    assert_no_secrets(portal, first, older)


def test_openapi_documents_every_api_v2_operation(portal):
    spec = portal.openapi()
    operations = [(path, method, op) for path, item in spec["paths"].items()
                  if path.startswith("/api/v2") for method, op in item.items()
                  if method in ("get", "post", "put", "patch", "delete")]
    assert len(operations) >= 100
    clashes = [name for name in spec["components"]["schemas"]
               if "__" in name and not name.startswith("Body_")]
    assert clashes == [], "two models share a name: the generated types would be mangled"
    for path, method, op in operations:
        ok = op["responses"].get("200") or op["responses"].get("201")
        assert ok is not None, (path, method)
        content = ok.get("content", {})
        assert content, (path, method, "a typed answer")
        for status in ("401", "403", "422"):
            schema = op["responses"][status]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ErrorResponse"), (path, method, status)
        if method in ("post", "put", "patch") and "requestBody" in op:
            body = op["requestBody"]["content"]["application/json"]["schema"]
            assert "$ref" in body, (path, method)
