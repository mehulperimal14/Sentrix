"""Accounts, households and the device profile (docs/7 §4.2, §5.11).

Owner: track T-ACCT. Foundation stub with the frozen signatures; the two
trivial helpers are implemented because every track relies on their rule.

Rules:

* Any change to what a profile contains (household, contacts, consents,
  PINs, window, safety defaults, channels, cameras, mode) calls
  :func:`bump_profile_version` **in the same transaction**, then (after the
  commit) ``hub.push_profile``. The hub never bumps the version.
* ``build_profile`` returns exactly the §5.11 JSON: never a password hash, a
  TOTP secret, a session id, contacts' e-mail addresses or recovery codes
  (the device refuses such a profile: ``sentrix.link.profile.validate``).
* T-ACCT warns in the wizard and on the household page when a contact has
  neither Telegram nor e-mail: the Portal cannot send SMS, so while the
  device is offline such a contact is unreachable.
* T-ACCT mints the Telegram link code (``mint_telegram_link``); T-CH's bot
  consumes it on ``/start <code>``.
"""

from __future__ import annotations

from typing import Any

from sentrix_portal.models import EmergencyContact, Household


def build_profile(db, household_id: str, device_id: str) -> dict[str, Any]:
    raise NotImplementedError("services.accounts.build_profile is track T-ACCT's")


def confirmed_contacts(db, household_id: str) -> list[EmergencyContact]:
    """The household's CONFIRMED contacts by priority: the only ones an SOS reaches."""
    return (db.query(EmergencyContact)
            .filter_by(household_id=household_id, consent_status="CONFIRMED")
            .order_by(EmergencyContact.priority, EmergencyContact.created_at).all())


def bump_profile_version(db, household_id: str) -> int:
    """+1 on ``households.profile_version`` inside the caller's transaction."""
    household = db.get(Household, household_id)
    if household is None:
        raise LookupError(f"unknown household {household_id}")
    household.profile_version = (household.profile_version or 0) + 1
    db.flush()
    return household.profile_version


def mint_telegram_link(db, *, subject_type: str, subject_id: str,
                       household_id: str) -> str:
    """A single-use ``telegram_link`` token for ``t.me/<bot>?start=<code>``."""
    raise NotImplementedError("services.accounts.mint_telegram_link is track T-ACCT's")


__all__ = ["build_profile", "confirmed_contacts", "bump_profile_version",
           "mint_telegram_link"]
