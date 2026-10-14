"""Company-side provisioning for the SENTRIX Portal (docs/7 §5.2; track T-ACCT).

Runs at the company, against the Portal's database and keys (the same
``PORTAL_DATABASE_URL`` / ``PORTAL_SECRET_KEY`` / ``PORTAL_DATA_DIR`` as
``python -m sentrix_portal``: the stored digests are HMACs under that key).

``device`` (the default) provisions one unit:

* a UUID4 device id and a serial (``SX5-000123``, the next free one unless
  given), state UNASSIGNED;
* a claim code (8 characters of the RFC 8628 alphabet, single use, 5 tries /
  15 min), a bootstrap secret and the one-time Wi-Fi setup code. The Portal
  keeps only their HMAC digests; the setup code goes into ``identity.json``
  as a SHA-256 digest (``/setup/wifi`` checks it on the device);
* ``<out>/identity.json`` (mode 0600): ``device_id``, ``serial``,
  ``bootstrap_secret``, ``setup_code_digest``, ``provisioned_at``. Write it
  to the SD card's ``/etc/sentrix`` (``--out /media/rootfs/etc/sentrix``):
  a factory reset wipes ``var/`` but keeps the identity. Refuses to overwrite
  an existing identity unless ``--force``;
* optionally a printable label (``--label label.html``: serial, claim code,
  setup code, QR to ``<portal>/claim?sn=<serial>``) and a pre-assignment to a
  customer (``--assign SX-0007``).

``admin`` creates a company_admin account (the first one, or another) and
prints its single-use activation code; TOTP is set up at the first sign-in.

The claim code, bootstrap secret, setup code and activation code are printed
once and never stored in clear. Nothing here sends a message or opens a
network connection.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IDENTITY_FILE = "identity.json"


def _portal():
    """(settings, database) of the Portal this tool provisions for."""
    from dotenv import load_dotenv

    from sentrix_portal import settings as portal_settings
    from sentrix_portal.db import Database

    load_dotenv(ROOT / ".env")
    settings = portal_settings.PortalSettings.from_env()
    portal_settings.set_current(settings)
    database = Database(settings.database_url)
    database.init()
    return settings, database


def _default_identity_dir() -> Path:
    from sentrix.link.profile import identity_dir
    return identity_dir()


def write_identity(directory: Path, identity: dict, *, force: bool = False) -> Path:
    """``<directory>/identity.json``, mode 0600, written atomically."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / IDENTITY_FILE
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists: this card already has an identity "
                              f"(use --force to replace it, which strands the old device id)")
    temporary = directory / f".{IDENTITY_FILE}.tmp"
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(identity, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:                                   # e.g. a FAT boot partition
        pass
    return path


def label_html(*, serial: str, claim_code: str, setup_code: str, claim_url: str,
               test_mode: bool) -> str:
    """A self-contained printable label (a local file, not a Portal page)."""
    from sentrix_portal.services.accounts import qr_svg
    banner = ('<p class="test">SENTRIX DEMO - TEST MODE, NOT A REAL EMERGENCY SERVICE</p>'
              if test_mode else "")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>SENTRIX label {html.escape(serial)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111; }}
.label {{ display: flex; gap: 1.5rem; align-items: center; border: 2px dashed #888;
          padding: 1rem 1.5rem; max-width: 34rem; }}
.code {{ font-family: ui-monospace, Consolas, monospace; font-size: 1.5rem; font-weight: 700;
         letter-spacing: 0.1em; }}
svg.qr {{ width: 9rem; height: 9rem; }}
.small {{ font-size: 0.8rem; color: #444; }}
.test {{ font-weight: 700; border: 2px solid #000; padding: 0.3rem; max-width: 34rem; }}
</style></head><body>
{banner}
<div class="label">
  <div>
    <h2>SENTRIX home unit</h2>
    <p>Serial <span class="code">{html.escape(serial)}</span></p>
    <p>Claim code <span class="code">{html.escape(claim_code)}</span></p>
    <p>Wi-Fi setup code <span class="code">{html.escape(setup_code)}</span></p>
    <p class="small">Scan to claim, or open {html.escape(claim_url)}</p>
  </div>
  <div>{qr_svg(f"{claim_url}#code={claim_code}")}</div>
</div>
</body></html>
"""


def provision(args) -> int:
    from sentrix.paths import VAR_DIR
    from sentrix_portal.models import Household
    from sentrix_portal.services import accounts

    settings, database = _portal()
    out = Path(args.out) if args.out else _default_identity_dir()
    if (out / IDENTITY_FILE).exists() and not args.force:
        print(f"refused: {out / IDENTITY_FILE} exists (use --force)", file=sys.stderr)
        return 2
    with database.session() as db:
        household = None
        if args.assign:
            household = db.query(Household).filter_by(
                customer_code=args.assign.strip().upper()).first()
            if household is None:
                print(f"refused: no customer {args.assign}", file=sys.stderr)
                return 2
        try:
            provisioned = accounts.provision_device(db, serial=args.serial, name=args.name,
                                                    actor_type="system")
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        if household is not None:
            accounts.assign_device(db, provisioned.device, household, actor_id=None,
                                   actor_type="system")
        path = write_identity(out, provisioned.identity, force=args.force)
        serial = provisioned.device.serial
        claim_url = f"{settings.base_url}/claim?sn={serial}"
        if args.label:
            Path(args.label).write_text(label_html(
                serial=serial, claim_code=provisioned.claim_code,
                setup_code=provisioned.setup_code, claim_url=claim_url,
                test_mode=settings.test_mode), encoding="utf-8")
    print(f"device id     {provisioned.identity['device_id']}")
    print(f"serial        {serial}")
    print(f"claim code    {provisioned.claim_code}")
    print(f"setup code    {provisioned.setup_code}")
    print(f"claim link    {claim_url}")
    print(f"identity      {path} (0600)")
    if args.label:
        print(f"label         {args.label}")
    if household is not None:
        print(f"assigned to   {household.customer_code}")
    try:
        inside_var = path.resolve().is_relative_to(VAR_DIR.resolve())
    except (OSError, ValueError):
        inside_var = False
    if inside_var:
        print("warning: the identity is inside var/, which a factory reset wipes. On the "
              "Pi write it to /etc/sentrix (--out).", file=sys.stderr)
    print("The codes above are shown once; the Portal keeps only their digests.")
    return 0


def create_admin(args) -> int:
    from sentrix_portal.services import accounts

    _, database = _portal()
    errors = accounts.FormErrors()
    username = accounts.clean_username(errors, "username", args.username)
    email = accounts.clean_email(errors, "email", args.email)
    if errors:
        print("refused: " + "; ".join(errors.values()), file=sys.stderr)
        return 2
    with database.session() as db:
        if accounts.username_taken(db, username):
            print(f"refused: username {username} is taken", file=sys.stderr)
            return 2
        user = accounts.create_staff_user(db, role="company_admin", username=username,
                                          display_name=args.name or username, email=email,
                                          created_by=None)
        code, _ = accounts.issue_activation_code(db, user, actor_type="system")
        url = accounts.activation_url(code)
    print(f"company admin {username}")
    print(f"activation    {code}")
    print(f"link          {url}")
    print("Single use, 72 h. TOTP is set up at the first sign-in.")
    return 0


COMMANDS = ("device", "admin")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "device")                      # the default command
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    device = commands.add_parser("device", help="provision a device (the default)")
    device.add_argument("--serial", help="SX5-000123 (default: the next free one)")
    device.add_argument("--name", help="optional device name")
    device.add_argument("--out", help="identity directory (default: the device's identity "
                                      "dir: SENTRIX_DEVICE_IDENTITY_DIR, /etc/sentrix, else "
                                      "var/link)")
    device.add_argument("--label", help="write a printable label (HTML) here")
    device.add_argument("--assign", metavar="SX-0007", help="pre-assign to this customer")
    device.add_argument("--force", action="store_true",
                        help="replace an existing identity.json")
    admin = commands.add_parser("admin", help="create a company_admin account")
    admin.add_argument("--username", required=True)
    admin.add_argument("--name")
    admin.add_argument("--email")
    args = parser.parse_args(argv)
    return create_admin(args) if args.command == "admin" else provision(args)


if __name__ == "__main__":
    raise SystemExit(main())
