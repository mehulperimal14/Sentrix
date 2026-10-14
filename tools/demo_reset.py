"""Create (or wipe and re-create) the demo world for the teachers' demo, in TEST MODE.

Design §6 as amended by the reviews (docs/7 §15): one household, flipped live
between the scenes, and most of the onboarding wizard pre-filled so that only
2-3 steps are shown live.

Everything lives in a dedicated demo directory (default ``var/demo``; any path
with ``--dir``), never in ``var/sentrix.db`` or the developer's Portal:

    <dir>/.sentrix-demo        marker: only a marked directory is ever wiped
    <dir>/portal/              Portal SQLite database and its key files
    <dir>/device/              the device's var/ tree (SENTRIX_VAR_DIR)
    <dir>/device/link/         identity.json (SENTRIX_DEVICE_IDENTITY_DIR)
    <dir>/demo.json            what tools/demo_run.py needs (mode 0600: it
                               holds the demo device's own LC password,
                               session secret and evidence key)
    <dir>/label.html           the device label (serial, claim code, QR)

What it creates (all fictional or team-consented, D12/DPDP note in the reviews):

* company admin ``demo_admin`` and Desk operator ``demo_desk``: password and
  **TOTP secret printed once** (add them to an authenticator app);
* customer **"Riya Sharma (demo)"** (``riya_demo``), active with a password, or
  pending with a single-use activation code (``--activation``) to show the
  first sign-in live;
* a **confirmed** emergency contact "Prashant (demo contact)";
* a provisioned device (identity file + label), assigned and claimed by the
  household, room "Living room";
* SOS PIN and duress PIN (printed once), the 30-minute window (the demo timer
  ``SENTRIX_SOS_DEMO_WINDOW_SECONDS``, default 90 s, is set by demo_run in
  test mode only), safety defaults on (``--safety-net off`` for the team
  default);
* ``--live-steps N`` leaves the last N wizard steps (of 5 device, 6 alerts and
  PIN, 7 consent and SOS preview) for the presenter to do live.

Phones, e-mails and Telegram chat ids of real team members come **only from
the environment** and are never written to the repository:
``SENTRIX_DEMO_OWNER_PHONE``, ``SENTRIX_DEMO_OWNER_EMAIL``,
``SENTRIX_DEMO_CONTACT_PHONE``, ``SENTRIX_DEMO_CONTACT_EMAIL``,
``SENTRIX_DEMO_CONTACT_TELEGRAM``. Without them fictional +91 90000 001xx
numbers are used. Nothing is ever sent: test mode is forced, and the send guard
admits only the ``SENTRIX_TEST_RECIPIENTS`` allowlist (also env only) and only
with both live switches, which this tool and demo_run never set.

Nothing here opens a camera, a microphone or a network connection.

    python tools/demo_reset.py                    # var/demo, fully onboarded
    python tools/demo_reset.py --live-steps 3     # steps 5-7 done live
    python tools/demo_reset.py --activation --live-steps 2 --safety-net off
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MARKER = ".sentrix-demo"
DEMO_FILE = "demo.json"
DEFAULT_DIR = ROOT / "var" / "demo"
LIVE_STEP_ORDER = (5, 6, 7)          # the steps --live-steps may leave undone
DEFAULT_WINDOW_SECONDS = 90

CUSTOMER_NAME = "Riya Sharma (demo)"
OWNER_USERNAME = "riya_demo"
ADMIN_USERNAME = "demo_admin"
OPERATOR_USERNAME = "demo_desk"
CONTACT_NAME = "Prashant (demo contact)"
#: Fictional numbers (never allowlisted, so never reachable).
FICTIONAL = {"owner": "+919000000101", "contact": "+919000000102"}
#: The consents a finished wizard records (step 7); medical notes and model
#: improvement stay off (D12, reviews).
OWNER_PURPOSES = ("account_sos", "face_recognition", "sos_snapshot", "remote_view",
                  "desk_live_view")


class DemoRefused(RuntimeError):
    """The demo directory is not safe to (re)create."""


@dataclass
class DemoWorld:
    """What :func:`reset` made. ``secrets`` are shown once and not stored,
    except the demo device's own LC secrets (they go to ``demo.json``)."""

    directory: Path
    info: dict[str, Any]
    secrets: dict[str, Any] = field(default_factory=dict)


# ── the directory ─────────────────────────────────────────────────────────────
def layout(directory: Path) -> dict[str, Path]:
    directory = Path(directory).resolve()
    return {"dir": directory, "portal": directory / "portal",
            "device": directory / "device", "identity": directory / "device" / "link",
            "demo": directory / DEMO_FILE, "label": directory / "label.html",
            "logs": directory / "logs"}


def check_directory(directory: Path) -> None:
    """Refuse anything but an empty/new directory or one this tool made: the
    real ``var/`` (or anything holding a ``sentrix.db`` without the marker) is
    never wiped."""
    directory = Path(directory).resolve()
    real_var = (ROOT / "var").resolve()
    if directory in (real_var, ROOT.resolve(), Path(directory.anchor)):
        raise DemoRefused(f"{directory} is not a demo directory (use a sub-directory "
                          f"such as var/demo)")
    if real_var.is_relative_to(directory):
        raise DemoRefused(f"{directory} contains the real var/ tree")
    if not directory.exists():
        return
    if not directory.is_dir():
        raise DemoRefused(f"{directory} is not a directory")
    if (directory / MARKER).exists():
        return
    if any(directory.iterdir()):
        raise DemoRefused(f"{directory} is not empty and was not made by demo_reset "
                          f"(no {MARKER}); choose another --dir")


def wipe(directory: Path) -> None:
    check_directory(directory)
    directory = Path(directory).resolve()
    if directory.exists():
        for child in directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MARKER).write_text("made by tools/demo_reset.py; wiped on every reset\n",
                                    encoding="utf-8")


def process_env(directory: Path, *, portal_url: str) -> dict[str, str]:
    """The environment a demo process runs with (set before importing sentrix):
    everything in the demo directory, test mode on, every live switch off."""
    paths = layout(directory)
    return {
        "SENTRIX_VAR_DIR": str(paths["device"]),
        "SENTRIX_DEVICE_IDENTITY_DIR": str(paths["identity"]),
        "PORTAL_DATA_DIR": str(paths["portal"]),
        "PORTAL_DATABASE_URL": f"sqlite:///{(paths['portal'] / 'portal.db').as_posix()}",
        "PORTAL_BASE_URL": portal_url,
        "PORTAL_SECRET_KEY": "", "PORTAL_DATA_KEY": "",    # key files in the demo dir
        "SENTRIX_TEST_MODE": "1", "PORTAL_TEST_MODE": "1", "SENTRIX_LIVE_ALERTS": "0",
        "SENTRIX_MODEM_LIVE": "0", "SENTRIX_TWILIO_ENABLED": "0",
        "SENTRIX_TELEGRAM_LIVE": "0", "SENTRIX_EMAIL_LIVE": "0", "SENTRIX_WEBPUSH_LIVE": "0",
    }


# ── secrets ───────────────────────────────────────────────────────────────────
def new_password() -> str:
    return "demo-" + secrets.token_urlsafe(9)


def new_pins(*, phone: str | None = None) -> tuple[str, str]:
    """(SOS PIN, duress PIN): 6 digits each, not weak, differing in >= 2 digits."""
    from sentrix_portal.services import accounts
    while True:
        sos = f"{secrets.randbelow(10 ** 6):06d}"
        duress = f"{secrets.randbelow(10 ** 6):06d}"
        if (accounts.pin_problem(sos, phone=phone, pincode="147001") is None
                and accounts.pin_problem(duress, phone=phone, pincode="147001") is None
                and accounts.digit_difference(sos, duress)
                >= accounts.sos_config().duress_min_digit_difference):
            return sos, duress


def _env_phone(name: str, fallback: str) -> str:
    from sentrix_portal.services import accounts
    raw = os.getenv(name, "").strip()
    return (accounts.normalize_phone(raw) or fallback) if raw else fallback


# ── the world ────────────────────────────────────────────────────────────────
def _staff(db, *, role: str, username: str, name: str, created_by: str | None):
    import pyotp  # noqa: F401  (fail early when the TOTP dependency is missing)

    from sentrix_portal import security
    from sentrix_portal.models import utcnow
    from sentrix_portal.services import accounts
    user = accounts.create_staff_user(db, role=role, username=username, display_name=name,
                                      email=f"{username}@example.com", created_by=created_by)
    password = new_password()
    secret = security.new_totp_secret()
    raw_codes, digests = security.new_recovery_codes()
    now = utcnow()
    user.password_hash = security.hash_password(password)
    user.password_changed_at = now
    user.status = "active"
    user.email_verified_at = now
    user.totp_secret_enc = security.seal(secret)
    user.totp_enabled = True
    user.recovery_codes = digests
    db.flush()
    return user, {"username": username, "password": password, "totp_secret": secret,
                  "totp_uri": security.totp_uri(secret, username),
                  "recovery_codes": raw_codes}


def reset(directory: Path, *, live_steps: int = 0, activation: bool = False,
          safety_net: bool = True, window_seconds: int = DEFAULT_WINDOW_SECONDS,
          portal_url: str = "http://portal.localhost:8100",
          lc_url: str = "http://127.0.0.1:8000") -> DemoWorld:
    """Wipe ``directory`` (if it is a demo directory) and build the demo world."""
    if not 0 <= int(live_steps) <= len(LIVE_STEP_ORDER):
        raise ValueError(f"--live-steps must be 0..{len(LIVE_STEP_ORDER)}")
    if window_seconds < 60:
        raise ValueError("the demo window must be at least 60 s (SosConfig)")
    paths = layout(directory)
    wipe(paths["dir"])
    for key in ("portal", "device", "identity", "logs"):
        paths[key].mkdir(parents=True, exist_ok=True)

    from sentrix_portal import security
    from sentrix_portal import settings as portal_settings
    from sentrix_portal.db import Database
    from sentrix_portal.models import (Camera, Consent, EmergencyContact, TelegramLink,
                                       utcnow)
    from sentrix_portal.services import accounts

    sys.path.insert(0, str(ROOT / "tools"))
    try:
        from provision_device import label_html, write_identity
    finally:
        sys.path.remove(str(ROOT / "tools"))

    settings = portal_settings.PortalSettings(
        database_url=f"sqlite:///{(paths['portal'] / 'portal.db').as_posix()}",
        data_dir=str(paths["portal"]), base_url=portal_url.rstrip("/"), test_mode=True,
        desk_number=(os.getenv("PORTAL_DESK_NUMBER", "").strip()
                     or os.getenv("SENTRIX_DESK_NUMBER", "").strip())).checked()
    previous = portal_settings._current
    portal_settings.set_current(settings)
    database = Database(settings.database_url)
    live = set(LIVE_STEP_ORDER[len(LIVE_STEP_ORDER) - int(live_steps):]) if live_steps else set()
    shown: dict[str, Any] = {}
    try:
        database.init()
        owner_phone = _env_phone("SENTRIX_DEMO_OWNER_PHONE", FICTIONAL["owner"])
        contact_phone = _env_phone("SENTRIX_DEMO_CONTACT_PHONE", FICTIONAL["contact"])
        owner_email = os.getenv("SENTRIX_DEMO_OWNER_EMAIL", "").strip() or "riya.demo@example.com"
        contact_email = os.getenv("SENTRIX_DEMO_CONTACT_EMAIL", "").strip() or None
        contact_chat = os.getenv("SENTRIX_DEMO_CONTACT_TELEGRAM", "").strip() or None
        with database.session() as db:
            admin, shown["admin"] = _staff(db, role="company_admin", username=ADMIN_USERNAME,
                                           name="Demo company admin", created_by=None)
            _, shown["operator"] = _staff(db, role="operator", username=OPERATOR_USERNAME,
                                          name="Demo Desk operator", created_by=admin.id)
            household, owner = accounts.create_customer(
                db, name=CUSTOMER_NAME, username=OWNER_USERNAME, email=owner_email,
                phone=owner_phone, city="Patiala", created_by=admin.id)
            now = utcnow()
            owner.email_verified_at = owner.phone_verified_at = now
            if activation:
                code, _ = accounts.issue_activation_code(db, owner, created_by=admin.id)
                shown["owner"] = {"username": OWNER_USERNAME, "activation_code": code,
                                  "activation_url": accounts.activation_url(code)}
            else:
                password = new_password()
                owner.password_hash = security.hash_password(password)
                owner.password_changed_at = now
                owner.status = "active"
                shown["owner"] = {"username": OWNER_USERNAME, "password": password}

            # steps 2-3: home and household (fictional address)
            household.house, household.street, household.locality = "12", "Mall Road", "Sector 4"
            household.landmark, household.city, household.state = ("near the SBI ATM",
                                                                   "Patiala", "Punjab")
            household.district, household.pincode = "Patiala", "147001"
            household.lat, household.lon = 30.3398, 76.3869
            household.entry_note = "Blue gate, first floor (demo)"
            household.occupants = {"adults": 2, "children": 0, "seniors": 1}
            household.nearest = {"police": {"name": "Civil Lines police station (demo)"},
                                 "hospital": {"name": "Rajindra Hospital (demo)"}}
            household.channels = {"email": True, "telegram": False, "sms": True}

            # step 4: one confirmed contact (consented team member or fictional)
            contact = EmergencyContact(household_id=household.id, name=CONTACT_NAME,
                                       relation="friend", phone_enc=security.seal(contact_phone),
                                       email=contact_email, priority=1, can_reach_30min=True,
                                       consent_status="CONFIRMED", confirmed_at=now)
            db.add(contact)
            db.flush()
            db.add(Consent(household_id=household.id, subject_type="contact",
                           subject_id=contact.id, purpose="account_sos",
                           notice_version=settings.notice_version, granted_at=now,
                           method="contact_page"))
            if contact_chat:
                contact.telegram_chat_id = contact_chat
                db.add(TelegramLink(subject_type="contact", subject_id=contact.id,
                                    household_id=household.id, chat_id=contact_chat))

            # the device: provisioned, assigned, and claimed unless step 5 is live
            provisioned = accounts.provision_device(db, name="Living room unit",
                                                    created_by=admin.id)
            device = provisioned.device
            accounts.assign_device(db, device, household, actor_id=admin.id)
            write_identity(paths["identity"], provisioned.identity, force=True)
            claim_url = f"{settings.base_url}/claim?sn={device.serial}"
            paths["label"].write_text(label_html(
                serial=device.serial, claim_code=provisioned.claim_code,
                setup_code=provisioned.setup_code, claim_url=claim_url, test_mode=True),
                encoding="utf-8")
            shown["device"] = {"serial": device.serial, "device_id": device.id,
                               "claim_code": provisioned.claim_code,
                               "setup_code": provisioned.setup_code, "claim_url": claim_url}
            if 5 not in live:
                outcome = accounts.claim_device(db, user=owner, serial=device.serial,
                                                code=provisioned.claim_code, ip=None)
                if not outcome.ok:
                    raise RuntimeError(f"demo claim failed: {outcome.error}")
                for camera in db.query(Camera).filter_by(device_id=device.id):
                    camera.room_label, camera.floor = "Living room", "First"

            # step 6: window, safety defaults, PINs
            if 6 not in live:
                household.sos_window_s = 1800
                household.safety_defaults = bool(safety_net)
                sos_pin, duress_pin = new_pins(phone=owner_phone)
                accounts.apply_pins(db, household, security.hash_pin(sos_pin),
                                    security.hash_pin(duress_pin), user=owner, ip=None)
                shown["pins"] = {"sos_pin": sos_pin, "duress_pin": duress_pin}
            else:
                household.safety_defaults = bool(safety_net)

            # step 7: consent and review
            if 7 not in live:
                for purpose in OWNER_PURPOSES:
                    db.add(Consent(household_id=household.id, subject_type="user",
                                   subject_id=owner.id, purpose=purpose,
                                   notice_version=settings.notice_version, granted_at=now,
                                   method="wizard"))
                household.onboarding_completed_at = now
            household.onboarding_step = min(live) if live else 7
            version = accounts.profile_changed(db, household.id, actor_id=admin.id,
                                               actor_type="admin", what="demo_reset")
            security.audit(db, "demo.reset", actor_type="system", household_id=household.id,
                           target_type="household", target_id=household.id,
                           detail={"test_mode": True, "live_steps": sorted(live),
                                   "activation": bool(activation),
                                   "safety_net": bool(safety_net),
                                   "window_seconds": int(window_seconds)})
            ids = {"household_id": household.id, "customer_code": household.customer_code,
                   "owner_id": owner.id, "contact_id": contact.id, "device_id": device.id,
                   "serial": device.serial, "profile_version": version}
    finally:
        database.dispose()
        portal_settings._current = previous

    device_secrets = {"lc_password": new_password(), "session_secret": secrets.token_hex(32),
                      "evidence_key": secrets.token_hex(32)}
    info = {
        "made_by": "tools/demo_reset.py", "test_mode": True, "created_at": _now_iso(),
        "portal_url": settings.base_url, "lc_url": lc_url.rstrip("/"),
        "window_seconds": int(window_seconds), "live_steps": sorted(live),
        "activation": bool(activation), "safety_net": bool(safety_net),
        "usernames": {"admin": ADMIN_USERNAME, "operator": OPERATOR_USERNAME,
                      "owner": OWNER_USERNAME},
        "customer_name": CUSTOMER_NAME, "contact_name": CONTACT_NAME,
        "paths": {k: str(v) for k, v in paths.items()},
        "env": process_env(paths["dir"], portal_url=settings.base_url),
        "device_secrets": device_secrets,
        **ids,
    }
    _write_private_json(paths["demo"], info)
    shown["lc"] = {"url": lc_url.rstrip("/"), "password": device_secrets["lc_password"]}
    return DemoWorld(paths["dir"], info, shown)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_private_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_demo(directory: Path) -> dict[str, Any]:
    """``demo.json`` of a demo directory (``DemoRefused`` when missing)."""
    path = layout(directory)["demo"]
    if not (Path(directory) / MARKER).exists() or not path.exists():
        raise DemoRefused(f"no demo in {directory}: run tools/demo_reset.py first")
    return json.loads(path.read_text(encoding="utf-8"))


# ── output ────────────────────────────────────────────────────────────────────
def describe(world: DemoWorld) -> str:
    info, shown = world.info, world.secrets
    allowlist = [p for p in os.getenv("SENTRIX_TEST_RECIPIENTS", "").replace(";", ",")
                 .replace("\n", ",").split(",") if p.strip()]
    lines = ["SENTRIX demo world (TEST MODE - nothing is sent)", "",
             f"directory      {world.directory}",
             f"Portal         {info['portal_url']}",
             f"Local Console  {info['lc_url']}", ""]
    for key, title in (("admin", "Company admin"), ("operator", "Desk operator")):
        staff = shown[key]
        lines += [f"{title} ({staff['username']})",
                  f"  password     {staff['password']}",
                  f"  TOTP secret  {staff['totp_secret']}",
                  f"  TOTP URI     {staff['totp_uri']}",
                  f"  recovery     {' '.join(staff['recovery_codes'])}"]
    owner = shown["owner"]
    lines.append(f"Customer {info['customer_name']} ({owner['username']})")
    if "password" in owner:
        lines.append(f"  password     {owner['password']}")
    else:
        lines += [f"  activation   {owner['activation_code']} (single use, 72 h)",
                  f"  link         {owner['activation_url']}"]
    if "pins" in shown:
        lines += [f"  SOS PIN      {shown['pins']['sos_pin']}",
                  f"  duress PIN   {shown['pins']['duress_pin']}"]
    device = shown["device"]
    lines += [f"Device {device['serial']} ({device['device_id']})",
              f"  claim code   {device['claim_code']}"
              + ("   <- step 5 is live" if 5 in info["live_steps"] else " (already claimed)"),
              f"  setup code   {device['setup_code']}",
              f"  label        {layout(world.directory)['label']}",
              f"Local Console password (device admin) {shown['lc']['password']}",
              "",
              f"Wizard steps left for the live demo: "
              f"{', '.join(map(str, info['live_steps'])) or 'none'}",
              f"Safety net (safety defaults): {'on' if info['safety_net'] else 'off'}",
              f"Demo window: {info['window_seconds']} s (test mode only)",
              f"Allowlist (SENTRIX_TEST_RECIPIENTS, env only): {len(allowlist)} entr"
              + ("y" if len(allowlist) == 1 else "ies")
              + "; live sending stays off (demo_run never sets the live switches).",
              "",
              "Passwords, TOTP secrets, PINs and codes above are shown ONCE; the Portal "
              "keeps only digests. Re-run demo_reset to start over.",
              "Next: python tools/demo_run.py"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="Test mode only. Writes nothing outside --dir.")
    parser.add_argument("--dir", default=str(DEFAULT_DIR),
                        help="demo directory (default var/demo; wiped on every reset)")
    parser.add_argument("--live-steps", type=int, default=0, choices=range(0, 4),
                        metavar="N", help="leave the last N wizard steps (5 device, 6 PIN "
                                          "and window, 7 consent) for the live demo")
    parser.add_argument("--activation", action="store_true",
                        help="leave the customer pending with an activation code")
    parser.add_argument("--safety-net", choices=("on", "off"), default="on",
                        help="the household's safety defaults (D9 default on)")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_SECONDS,
                        help="demo SOS window in seconds (>= 60; test mode only)")
    parser.add_argument("--portal-url", default="http://portal.localhost:8100")
    parser.add_argument("--lc-url", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    directory = Path(args.dir)
    if "sentrix" not in sys.modules:
        # Before any sentrix import: every path into the demo dir, test mode on.
        os.environ.update(process_env(directory, portal_url=args.portal_url))
    try:
        world = reset(directory, live_steps=args.live_steps, activation=args.activation,
                      safety_net=args.safety_net == "on", window_seconds=args.window,
                      portal_url=args.portal_url, lc_url=args.lc_url)
    except (DemoRefused, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(describe(world))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
