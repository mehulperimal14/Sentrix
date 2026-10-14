"""Outbound channels behind one router and the send guard (docs/7 §8).

Track T-CH adds ``router.py`` (the device service: ``build_service``),
``telegram.py``, ``email.py``, ``modem.py`` and ``twilio.py`` next to
``base.py``. Every transport is reached only through ``base.Channel``, which
asks ``sentrix.alerting.guard`` first.
"""

from sentrix.alerting.channels.base import (Button, Channel, ChannelRouter,
                                            FakeTransport, MemoryOutbox, Message,
                                            Outbox, OutboxItem, Recipient,
                                            SendResult, Transport)

__all__ = ["Button", "Channel", "ChannelRouter", "FakeTransport", "MemoryOutbox",
           "Message", "Outbox", "OutboxItem", "Recipient", "SendResult", "Transport"]
