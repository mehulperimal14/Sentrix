"""The device's outbound link to the Portal (docs/7 §5).

Foundation: ``protocol.py`` (message types, envelopes, binary frames) and
``tunnel_auth.py`` (the in-process auth bridge). Track T-LINK adds
``client.py`` (``build_service``: WSS client, outbox pump, heartbeat,
subscriptions), ``tunnel.py`` (in-process executor over ``httpx.ASGITransport``),
``commands.py`` (device-enforced command rules) and ``profile.py`` (encrypted
profile cache).
"""
