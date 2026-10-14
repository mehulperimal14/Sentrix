"""Portal domain services, importable by routes and workers of every track.

Planned modules (docs/7 §2, §4.3): ``accounts.py`` (T-ACCT: users, activation,
households, profile building), ``hub.py`` and ``sos.py`` (T-LINK: connected
devices, commands, the cloud SOS authority; ``sos.respond(...)`` is the one
entry point Telegram callbacks and action links call), ``notify.py`` (T-CH:
the Portal channel router and its worker).
"""
