"""Retention caps on the Portal (decisions, data protection; docs/7 §6.2).

Owner: track T-ACCT. A background module (``start(app) -> stop``) that prunes
daily: SOS snapshots after 30 days, incident mirrors of unreviewed incidents
after 180 days, TRUE after 1 year, audit rows after 1 year (the chain records
a signed "retention" boundary row so ``verify_audit_chain`` reports a gap,
not tampering), declined contacts within 7 days, and erasure on household
closure. The caps (``sentrix.config.RetentionConfig``) override every
exemption. Foundation stub.
"""

from __future__ import annotations

from typing import Any


def prune(db, now=None) -> dict[str, Any]:
    """One pass; returns counts per table."""
    return {}


def start(app):
    return lambda: None


__all__ = ["prune", "start"]
