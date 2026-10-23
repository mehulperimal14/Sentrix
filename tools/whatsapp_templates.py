"""The three WhatsApp message templates SENTRIX sends (D17): show, create, check.

WhatsApp lets a business start a conversation only with a template Meta has
approved. SENTRIX uses three (``sentrix/alerting/channels/whatsapp.py``,
``TEMPLATES``): ``sentrix_prompt`` (the owner's question, three quick
replies), ``sentrix_contact`` (a contact's alert, two) and
``sentrix_notice`` (no buttons). All are UTILITY templates in English.

    python tools/whatsapp_templates.py show      # what would be created (no network)
    python tools/whatsapp_templates.py create    # create them in your WhatsApp account
    python tools/whatsapp_templates.py status    # Meta's review state of each

``create`` and ``status`` need ``WHATSAPP_ACCESS_TOKEN`` and
``WHATSAPP_WABA_ID`` (the WhatsApp Business Account id from the app's
WhatsApp > API Setup page) in the environment or ``.env``. They talk to
Meta's template API only: no message is sent to anyone. A template that
already exists is reported, not changed (edit or delete it in WhatsApp
Manager).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GRAPH = "https://graph.facebook.com"


def definitions(language: str | None = None) -> list[dict]:
    """The Graph API bodies of the three templates."""
    from sentrix.alerting.channels import whatsapp as W
    language = language or W.template_language()
    out = []
    for kind, spec in W.TEMPLATES.items():
        components: list[dict] = [{"type": "BODY", "text": spec["body"],
                                   "example": {"body_text": [[spec["example"]]]}}]
        if spec["buttons"]:
            components.append({"type": "BUTTONS", "buttons": [
                {"type": "QUICK_REPLY", "text": label} for label, _ in spec["buttons"]]})
        out.append({"name": W.template_name(kind), "category": "UTILITY",
                    "language": language, "components": components})
    return out


def _credentials() -> tuple[str, str]:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    waba = os.getenv("WHATSAPP_WABA_ID", "").strip()
    if not token or not waba:
        raise SystemExit("set WHATSAPP_ACCESS_TOKEN and WHATSAPP_WABA_ID (environment or .env)")
    return token, waba


def _client():
    import httpx
    return httpx.Client(timeout=20.0)


def create() -> int:
    from sentrix.alerting.channels import whatsapp as W
    token, waba = _credentials()
    url = f"{GRAPH}/{W.api_version()}/{waba}/message_templates"
    headers = {"Authorization": f"Bearer {token}"}
    failed = 0
    with _client() as client:
        for body in definitions():
            response = client.post(url, json=body, headers=headers)
            data = response.json() if response.content else {}
            if response.status_code < 400:
                print(f"{body['name']:18s} created: {data.get('status', '?')} "
                      f"({data.get('category', body['category'])})")
                continue
            error = data.get("error") or {}
            message = error.get("error_user_msg") or error.get("message") or response.text
            if "already exists" in str(message).lower() or error.get("code") == 2388024:
                print(f"{body['name']:18s} already exists (unchanged)")
                continue
            failed += 1
            print(f"{body['name']:18s} refused ({response.status_code}): {message}"[:300])
    print("Meta reviews each template, usually within minutes; run 'status' to check.")
    return 1 if failed else 0


def status() -> int:
    from sentrix.alerting.channels import whatsapp as W
    token, waba = _credentials()
    wanted = {body["name"] for body in definitions()}
    url = f"{GRAPH}/{W.api_version()}/{waba}/message_templates"
    with _client() as client:
        response = client.get(url, params={"fields": "name,status,language,category",
                                           "limit": 200},
                              headers={"Authorization": f"Bearer {token}"})
    if response.status_code >= 400:
        print(f"refused ({response.status_code}): {response.text[:300]}")
        return 1
    found = {row["name"]: row for row in response.json().get("data", [])
             if row.get("name") in wanted}
    for name in sorted(wanted):
        row = found.get(name)
        print(f"{name:18s} " + (f"{row.get('status')} ({row.get('language')}, "
                                f"{row.get('category')})" if row else "missing"))
    approved = all((found.get(n) or {}).get("status") == "APPROVED" for n in wanted)
    if not approved:
        print("Until all three are APPROVED, set WHATSAPP_MODE=auto (or session): alerts "
              "then need the phone to have messaged the SENTRIX number in the last 24 h.")
    return 0 if approved else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("show", "create", "status"))
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    if args.command == "show":
        print(json.dumps(definitions(), indent=2, ensure_ascii=False))
        return 0
    return create() if args.command == "create" else status()


if __name__ == "__main__":
    sys.exit(main())
