"""The Portal's SOS: mirror, cloud authority and every SOS message (docs/7 §7.6, §8).

Owner: track T-LINK. Foundation stub with the frozen signatures.

What this module owns on the Portal:

* the mirror of device requests and events (from ``sos.*`` outbox events,
  applied in per-origin seq order, gaps buffered);
* the cloud authority while a device is offline (heartbeat lost 45 s):
  timers, expiry, and requests the cloud opens itself (``authority_side =
  "cloud"``, flag ``device_offline``, origin cloud) when the heartbeat is lost
  during an open L2+ incident; ``sos.sync`` on reconnect;
* **which event goes to which recipient at which step** (docs/7 §7.5 steps,
  ``sentrix.sos.states``), minting the ``/a/`` and ``/c/`` link tokens and
  the Telegram buttons (``channels.base.callback_data``), rendering the text
  from ``sentrix/sos/templates.py`` (T-SOS; the one English source both
  sides import), and handing each message to ``notify.enqueue`` (T-CH owns
  transports, outbox, worker and inbound Telegram, which calls ``respond``);
* :func:`respond`: the one entry point for SOS answers from Portal pages,
  action links, contact pages and Telegram callbacks. Online, it sends the
  ``sos.respond`` / ``sos.update`` command and returns the device's result;
  offline, the cloud applies it itself (PIN checked against the household's
  hashes, duress included) and syncs later.

``actor`` is ``sentrix.actor.Actor.to_dict()``. The result has the shape of
``sentrix.sos.SosResult`` (``request`` in the actor's view: owner view for
owners and members).
"""

from __future__ import annotations

from typing import Any

#: Portal-side actions: the owner's (sentrix.sos.RESPOND_ACTIONS) plus the
#: Desk's and contacts'.
ACTIONS = ("approve", "decline", "checking", "safe", "help_now", "acknowledge",
           "resolve")


async def respond(db, request_id: str, action: str, *, actor: dict, channel: str,
                  authority: str | None = None, pin: str | None = None,
                  detail: dict | None = None) -> dict[str, Any]:
    return {"ok": False, "code": "unsupported", "status": 400,
            "message": "the Portal SOS service is not installed yet", "request": None}


def open_requests(db, household_id: str) -> list[dict[str, Any]]:
    """The household's open requests, owner view, newest first."""
    return []


def start(app):
    """Background: cloud timers for requests whose device is offline."""
    return lambda: None


__all__ = ["ACTIONS", "respond", "open_requests", "start"]
