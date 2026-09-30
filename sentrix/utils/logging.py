"""Structured logging and per-engine telemetry.

The previous ``instrumentation.log_instrumentation`` no-opped unless
``SENTRIX_EVAL_MODE=1`` was set, so the "per-engine latency and inference
logging" listed as operational in the docs produced nothing in normal
operation. Telemetry here is always on, buffered in memory for the dashboard
and written as JSON Lines to ``var/logs/``.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Any

from sentrix.paths import LOG_DIR

_CONFIGURED = False


def configure(level: int = logging.INFO) -> None:
    """Install a single stderr handler. Safe to call more than once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger("sentrix")
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(f"sentrix.{name}")


class Telemetry:
    """Rolling latency statistics per component, plus a JSONL audit trail."""

    def __init__(self, window: int = 200) -> None:
        self._lock = threading.Lock()
        self._latencies: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))
        self._counts: dict[str, int] = defaultdict(int)
        self._fh = None
        self._log = get_logger("telemetry")

    def _handle(self):
        if self._fh is None:
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                self._fh = open(LOG_DIR / f"telemetry_{stamp}.jsonl", "a",
                                encoding="utf-8")
            except OSError as exc:
                self._log.warning("telemetry file unavailable: %s", exc)
                self._fh = False          # sentinel: stop retrying
        return self._fh or None

    def record(self, component: str, event: str, data: dict[str, Any] | None = None,
               latency: float | None = None) -> None:
        with self._lock:
            self._counts[f"{component}.{event}"] += 1
            if latency is not None:
                self._latencies[component].append(latency)
            handle = self._handle()
            if handle is None:
                return
            entry = {"ts": round(time.time(), 3), "component": component,
                     "event": event}
            if latency is not None:
                entry["latency_ms"] = round(latency * 1000, 2)
            if data:
                entry["data"] = data
            try:
                handle.write(json.dumps(entry, default=str) + "\n")
                handle.flush()
            except (OSError, TypeError):
                pass

    def stats(self) -> dict[str, dict[str, float]]:
        """Mean/p95 latency in milliseconds, per component."""
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for component, samples in self._latencies.items():
                if not samples:
                    continue
                ordered = sorted(samples)
                idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
                out[component] = {
                    "mean_ms": round(1000 * sum(ordered) / len(ordered), 2),
                    "p95_ms": round(1000 * ordered[idx], 2),
                    "n": len(ordered),
                }
        return out

    def close(self) -> None:
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except OSError:
                    pass
            self._fh = None


class timed:
    """Context manager recording elapsed time against a component.

    >>> with timed("detector", "inference"):
    ...     run_model()
    """

    __slots__ = ("component", "event", "data", "_start")

    def __init__(self, component: str, event: str = "inference",
                 data: dict[str, Any] | None = None) -> None:
        self.component = component
        self.event = event
        self.data = data
        self._start = 0.0

    def __enter__(self) -> "timed":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> bool:
        telemetry.record(self.component, self.event, self.data,
                         latency=time.perf_counter() - self._start)
        return False


telemetry = Telemetry()

__all__ = ["configure", "get_logger", "telemetry", "timed", "Telemetry"]
