"""The SOS service's own thread: queued work at once, timers on time (docs/7 §3.6).

The pipeline hands the SOS service its triggers and level changes on the
inference thread and the FIFO worker, the channel router reports delivery on
its worker, and the verdict listener fires on whichever thread set the label.
None of them may block, so they only enqueue and :meth:`Sweeper.wake` this
thread. It then runs the engine's ``work()`` callable, which drains the queue
and applies every timer that is due (reminders, lapse warning, contacts,
expiry, re-pages, stale close), and sleeps until the next timer or at most
``settings.sos.sweep_interval_seconds`` (15 s; the engine also sweeps once at
start-up, in :meth:`SosEngine.start`).

Nothing here knows about requests: ``work`` returns the seconds until its next
due timer (or None), and a failing ``work`` is logged and retried on the next
period, so a database hiccup cannot stop the timers for good.
"""

from __future__ import annotations

import threading
from typing import Callable

from sentrix.utils.logging import get_logger

log = get_logger("sos.sweeper")


class Sweeper:
    """A daemon thread calling ``work()`` whenever woken and at least every
    ``interval`` seconds. ``start``/``stop`` are idempotent; ``stop`` joins."""

    def __init__(self, work: Callable[[], float | None], interval: float = 15.0, *,
                 name: str = "sentrix-sos") -> None:
        self._work = work
        self.interval = max(0.05, float(interval))
        self.name = name
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.runs = 0
        self.failures = 0
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.name)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            delay = self.interval
            try:
                next_in = self._work()
                self.runs += 1
                if next_in is not None:
                    delay = min(delay, max(0.05, float(next_in)))
            except Exception as exc:                  # timers must survive a bad sweep
                self.failures += 1
                self.last_error = str(exc)
                log.error("SOS sweep failed: %s", exc, exc_info=True)
            self._wake.wait(delay)


__all__ = ["Sweeper"]
