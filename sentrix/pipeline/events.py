"""The event log: one JSONL row per scored frame.

This is the most valuable artifact the running system produces, and it did not
previously exist. Each row holds the complete feature vector, the TCI, the
committed level and the incident id. That single file serves three purposes:

1. **Audit trail** — exactly what the system saw and decided, replayable.
2. **Fusion training set** — the notebook that was supposed to retrain the TCI
   model stopped with "no real multimodal ground-truth dataset was found".
   Running the pipeline over labelled clips writes precisely that dataset here.
   An hour of footage at 6 Hz is ~21,000 labelled rows.
3. **Evaluation input** — false-alarm rate per hour is computed directly from
   these rows, which is the headline claim of the whole project.

Rows are appended, never rewritten, and each file is stamped with the session
id so a replay run never mixes with a live run.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from sentrix.fusion.features import CONTRACT_V2
from sentrix.paths import EVENTLOG_DIR
from sentrix.utils.logging import get_logger

log = get_logger("events")

SCHEMA_VERSION = 2


class EventLog:
    """Append-only JSONL writer, one file per session."""

    def __init__(self, session_id: str | None = None,
                 directory: Path | None = None, source: str = "live") -> None:
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.directory = directory or EVENTLOG_DIR
        self.source = source
        self._lock = threading.Lock()
        self._handle = None
        self._rows = 0
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.path = self.directory / f"events_{stamp}_{self.session_id}.jsonl"

    def _open(self):
        if self._handle is None:
            try:
                self._handle = open(self.path, "a", encoding="utf-8")
                log.info("event log: %s", self.path)
            except OSError as exc:
                log.error("cannot open event log %s: %s", self.path, exc)
                self._handle = False
        return self._handle or None

    def write(self, result, level: int, status: str,
              incident_id: str | None = None, latency: float = 0.0,
              label: str | None = None, extra: dict[str, Any] | None = None) -> None:
        """Record one scored frame.

        ``label`` is the optional ground-truth annotation used when replaying a
        labelled clip; live runs leave it None and it is filled in later.
        """
        features = result.features
        row = {
            "schema": SCHEMA_VERSION,
            "session": self.session_id,
            "source": self.source,
            "ts": round(time.time(), 3),
            "seq": self._rows,
            "incident_id": incident_id,
            "tci": round(result.tci, 4),
            "raw_tci": round(result.raw_tci, 4),
            "level": level,
            "status": status,
            "incident_type": result.incident_type,
            "fusion_source": result.source,
            "uncertainty": round(result.uncertainty, 4),
            "latency_ms": round(latency * 1000, 2),
            "features": {name: round(getattr(features, name), 5)
                         for name in CONTRACT_V2},
            "context": {
                "authorized": features.authorized,
                "person_present": features.person_present,
                "person_count": features.person_count,
                "behaviour_label": features.behaviour_label,
                "audio_label": features.audio_label,
                "identity_degraded": features.identity_degraded,
            },
        }
        if label is not None:
            row["label"] = label
        if extra:
            row["extra"] = extra

        with self._lock:
            self._rows += 1
            handle = self._open()
            if handle is None:
                return
            try:
                handle.write(json.dumps(row, default=str) + "\n")
                if self._rows % 50 == 0:
                    handle.flush()
            except (OSError, TypeError) as exc:
                log.debug("event write failed: %s", exc)

    @property
    def rows(self) -> int:
        return self._rows

    def close(self) -> None:
        with self._lock:
            if self._handle:
                try:
                    self._handle.flush()
                    self._handle.close()
                except OSError:
                    pass
            self._handle = None


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    """Stream rows from an event log, skipping any that are malformed."""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_dataset(directory: Path | None = None):
    """Load every event log in a directory into a pandas DataFrame.

    Feature columns are flattened to top level, so the result is directly
    usable as ``X`` for fusion training.
    """
    import pandas as pd

    directory = directory or EVENTLOG_DIR
    rows: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("events_*.jsonl")):
        for row in read_events(path):
            flat = {k: v for k, v in row.items()
                    if k not in ("features", "context", "extra")}
            flat.update(row.get("features", {}))
            flat.update({f"ctx_{k}": v for k, v in row.get("context", {}).items()})
            flat["log_file"] = path.name
            rows.append(flat)
    return pd.DataFrame(rows)


__all__ = ["EventLog", "read_events", "load_dataset", "SCHEMA_VERSION"]
