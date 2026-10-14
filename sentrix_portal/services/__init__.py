"""Portal domain services, importable by routes and workers of every track.

Signatures are frozen (docs/7 §4.2); the foundation ships stubs so tracks can
import and monkeypatch them before their owner lands:

* ``accounts.py`` (T-ACCT): ``build_profile``, ``confirmed_contacts``,
  ``bump_profile_version``, ``mint_telegram_link``.
* ``hub.py`` (T-LINK): ``is_online`` (sync), ``send_command``,
  ``push_profile``, ``issue_token``, ``unpair`` (async); background watcher.
* ``sos.py`` (T-LINK): ``respond`` (async; the one entry point for Portal
  pages, action links, contact pages and Telegram callbacks),
  ``open_requests``; who gets which SOS message at which step; cloud timers.
* ``notify.py`` (T-CH): ``router``, ``enqueue``; delivery worker, Telegram
  polling.
* ``retention.py`` (T-ACCT): retention caps; background prune.

Modules with ``start(app) -> stop`` are listed in ``app.BACKGROUND_MODULES``.
"""
