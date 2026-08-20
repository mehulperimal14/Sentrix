# Database Audit

This document summarizes the active database layer used by the live app. The authoritative assessment is also captured in [SENTRIX_MASTER_TECHNICAL_REPORT.md](../SENTRIX_MASTER_TECHNICAL_REPORT.md).

## Current Database Model
The repository uses synchronous SQLite with SQLAlchemy in [db/database.py](../db/database.py) and [db/models.py](../db/models.py).

### Schema summary
- `AuthorizedPerson`: enrolled face identities.
- `EventLog`: threat audit rows.
- `DispatchPackageModel`: emergency dispatch packages.

## Current Lifecycle
1. Create engine and sessionmaker at import time.
2. Create tables via `init_db()`.
3. Open sessions with `get_session()`.
4. Commit or rollback inside a context manager.
5. Convert ORM rows into dicts for route responses.

## Strengths
- Very simple to understand.
- Explicit transactional scope.
- Reasonable for a local demo or capstone appliance.

## Weaknesses
- Synchronous ORM in async request handlers.
- SQLite single-writer ceiling.
- No indexes on hot predicates.
- Helper functions return presentation dicts rather than a proper repository abstraction.
- JSON is stored as text instead of a queryable JSON column.

## Hot Query Paths
- `get_recent_events()` sorts by newest `created_at`.
- `get_latest_pending_dispatch()` filters by `status='PENDING'` and sorts by `created_at`.
- `log_event()` inserts an audit row for qualifying threat events.
- `save_dispatch_package()` stores emergency dispatch payloads.
- `update_dispatch_status()` updates a dispatch row in place.

## Operational Verdict
The current schema is fine for the prototype stage, but it needs indexes, retention, and eventually a more scalable database strategy before production use.
