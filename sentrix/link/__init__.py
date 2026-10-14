"""The device's outbound link to the Portal (docs/7 §5).

Foundation: ``protocol.py`` (message types, envelopes, binary frames,
canonical JSON), ``tunnel_auth.py`` (the in-process auth bridge),
``profile.py`` (profile accessor and encrypted cache) and :func:`publish`
below. Track T-LINK adds ``client.py`` (``build_service``: WSS client, outbox
pump, heartbeat, subscriptions), ``tunnel.py`` (in-process executor over
``httpx.ASGITransport``), ``commands.py`` (device-enforced command rules) and
``policy.py`` (the tunnel path table).

Every track that has something for the Portal calls :func:`publish`, never
``link_store.enqueue`` directly: with the link off (the default) nothing is
stored, so the outbox cannot grow without anyone to acknowledge it.
"""

from __future__ import annotations

from typing import Any

from sentrix.utils.logging import get_logger

log = get_logger("link")


def link_enabled() -> bool:
    """``SENTRIX_LINK_ENABLED`` (tests monkeypatch this function)."""
    from sentrix.config import settings
    return bool(settings.link.enabled)


def publish(kind: str, payload: dict[str, Any], *,
            dedupe_key: str | None = None) -> int | None:
    """Queue one outbox event for the Portal (at-least-once, docs/7 §5.5).

    Returns its seq, or None: link off, duplicate ``dedupe_key``, unknown
    ``kind``, or a storage error (logged). Never raises. SOS kinds jump the
    queue (priority 0); use ``protocol.sos_dedupe_key(request, origin, seq)``
    for them, and publish device-origin request events only (cloud events
    came from the Portal and never go back up).
    """
    if not link_enabled():
        return None
    from sentrix.link import protocol
    if kind not in protocol.EVENT_KINDS:
        log.error("publish: unknown event kind %r", kind)
        return None
    try:
        from sentrix.storage import link_store
        priority = (link_store.PRIORITY_SOS if kind in protocol.SOS_EVENT_KINDS
                    else link_store.PRIORITY_NORMAL)
        return link_store.enqueue(kind, payload, priority=priority, dedupe_key=dedupe_key)
    except Exception as exc:
        log.error("publish %s failed: %s", kind, exc)
        return None


__all__ = ["publish", "link_enabled"]
