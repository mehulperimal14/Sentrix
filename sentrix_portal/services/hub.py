"""Connected devices: the link hub's Python API (docs/7 §4.2).

Owner: track T-LINK. The foundation ships these signatures (frozen) with
"every device is offline" bodies, so T-ACCT, T-CH and the SOS pages can import
and monkeypatch them before the hub exists.

Threading: one Portal worker process; the hub keeps its connections in memory
on the event loop. ``is_online`` is a plain function (a dict lookup); the
rest are coroutines and must be awaited from the loop.

* ``send_command`` -> ``{ok, code, status, message, result, queued,
  command_id}``. Online: the command goes out with ``expires_at = now +
  timeout`` and the device's ``command.result`` is awaited (``timeout`` code
  after ``timeout`` s). Offline with ``queue_if_offline``: a QUEUED
  ``device_commands`` row (``queued: True``, code ``device_offline``) sent
  after the next ``welcome`` ("queued, will sync").
* ``push_profile`` -> the version pushed (``households.profile_version``,
  which T-ACCT increments in the same transaction as the change; the hub
  never bumps it), built by ``accounts.build_profile``; 0 when offline (it
  goes out after the next ``welcome``).
* ``issue_token`` sends ``token.issue`` now, or right after the device's next
  bootstrap ``hello``; stores only ``token_digest``.
* ``unpair`` sends ``unpair`` (the device wipes token and profile), revokes
  the token, closes the socket with 4003 and audits ``device.unpaired``.
"""

from __future__ import annotations

from typing import Any


def is_online(device_id: str) -> bool:
    return False


def _offline(message: str, *, queued: bool = False) -> dict[str, Any]:
    return {"ok": False, "code": "device_offline", "status": 503, "message": message,
            "result": None, "queued": queued, "command_id": None}


async def send_command(device_id: str, name: str, args: dict, actor: dict, *,
                       timeout: float = 10.0, queue_if_offline: bool = True) -> dict[str, Any]:
    """``actor`` is ``sentrix.actor.Actor.to_dict()`` of the Portal user."""
    return _offline("the link hub is not installed yet")


async def push_profile(db, device_id: str) -> int:
    return 0


async def issue_token(db, device_id: str) -> None:
    return None


async def unpair(db, device_id: str, actor: dict) -> dict[str, Any]:
    return _offline("the link hub is not installed yet")


def start(app):
    """Background: heartbeat watcher (``heartbeat_lost_seconds``) -> device
    offline, ``device.offline`` audit, cloud SOS authority (services.sos)."""
    return lambda: None


__all__ = ["is_online", "send_command", "push_profile", "issue_token", "unpair", "start"]
