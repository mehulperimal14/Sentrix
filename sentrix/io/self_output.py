"""When the device itself is making a sound (D15).

The siren is heard by the device's own microphone. A window of that audio
must not vote: a siren is a siren and says nothing about the room. (The
device has no buzzer and no talk-down speaker since D19.) Every sound
source marks the interval it plays (monotonic clock, the same as
the audio windows); ``AudioEngine.classify_window`` drops a window that
overlaps one (``model="self"``, alignment status ``self_output``).

Thread-safe and in-process only. Tests reset it with :func:`clear`.
"""

from __future__ import annotations

import threading
import time
from collections import deque

#: Seconds added on both sides of a marked interval (player start-up, the
#: room's echo, timer jitter).
MARGIN_SECONDS = 0.5

_lock = threading.Lock()
_intervals: deque[list] = deque(maxlen=64)       # [start, end or None, source]


def begin(source: str, at: float | None = None) -> list:
    """An interval that has started and not yet ended; pass it to :func:`end`."""
    item = [time.monotonic() if at is None else float(at), None, str(source)]
    with _lock:
        _intervals.append(item)
    return item


def end(item: list, at: float | None = None) -> None:
    with _lock:
        item[1] = time.monotonic() if at is None else float(at)


def mark(source: str, seconds: float, at: float | None = None) -> None:
    """A sound of known length starting now (or at ``at``)."""
    start = time.monotonic() if at is None else float(at)
    with _lock:
        _intervals.append([start, start + max(0.0, float(seconds)), str(source)])


def overlapping(start: float, finish: float, margin: float = MARGIN_SECONDS) -> str | None:
    """The source of a marked interval overlapping ``[start, finish]``, if any
    (an interval still playing counts as running until now)."""
    now = time.monotonic()
    with _lock:
        items = list(_intervals)
    for begin_at, end_at, source in reversed(items):
        stop = now if end_at is None else end_at
        if begin_at - margin <= finish and stop + margin >= start:
            return source
    return None


def clear() -> None:
    with _lock:
        _intervals.clear()


__all__ = ["MARGIN_SECONDS", "begin", "clear", "end", "mark", "overlapping"]
