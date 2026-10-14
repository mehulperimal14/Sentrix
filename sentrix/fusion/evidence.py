"""Per-class threat evidence with temporal confirmation.

Why this exists
---------------
The first live run (CLAUDE.md §5) produced ~171 false weapon alarms per hour.
Every one came from a single tick of the scene classifier a hair over its
threshold, in a room it had never seen; a single frame was enough because the
old TCI engine turned any crossing into a hard override. This module is the
only place threat evidence is formed, and it applies three rules before any
source may count:

1. **Confirmation counted in observations.** A source's crossings for a class
   count only once they occur on ``confirm_k`` of the last ``confirm_n`` ticks
   (default 2 of 3). Counting ticks rather than seconds keeps the rule honest
   at 0.5 Hz, where a 0.5 s window held a single frame.
2. **A weapon needs a person.** Weapon crossings count only if a person was
   seen in the last ``person_memory_seconds``.
3. **Escalation caps by corroboration.** One confirmed source lands in the L3
   band (evidence and snapshot, no SMS or siren). A classifier source *and* a
   detector box, both confirmed, move to their own scale: L4 at the threshold,
   L5 once the weaker of the two is well over it. Experimental classes
   (assault, robbery) stop at L2.

Identity never relaxes threat evidence. Frame-level identity says only that
some face matched, so a stranger next to a resident would inherit the
resident's authorisation; per-track identity (CLAUDE.md M4) is the
precondition for any such rule. Replaying the live session showed the
relaxation bought nothing there.

Sources
-------
Each tick the pipeline hands in ``sources``: ``{source_name: {class: (value,
threshold[, fresh])}}``. ``scene`` and ``violence`` are classifier sources;
``detector`` is the YOLO heads. Which kinds vote is ``ThreatConfig.sources``.

Only *fresh* values - a model evaluation made on this tick - are pushed into
the confirmation window. A cached value (a model that runs every other tick,
the violence model between hops, a failed inference) is carried with the
window's current state and never counts as a new observation, so one
evaluation cannot confirm itself. A value that is not finite, or a threshold
outside (0, 1), is recorded as ``gated`` and contributes nothing.

The resulting per-class ``score`` is on the TCI scale and is what the TCI engine
takes the maximum of — so any event row can be re-derived by hand from the votes
it records.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from sentrix.config import settings
from sentrix.utils.logging import get_logger

log = get_logger("evidence")

#: Tie-break order when two classes score the same (mirrors alarm severity).
CLASS_ORDER: tuple[str, ...] = ("fire", "weapon", "assault", "robbery")
CLASSIFIER_SOURCES = frozenset({"scene", "violence"})
DETECTOR_SOURCES = frozenset({"detector"})


class ConfirmationWindow:
    """k-of-n crossings over the last n observations; a long gap resets it."""

    def __init__(self, k: int, n: int, max_gap_seconds: float) -> None:
        self.n = max(1, int(n))
        self.k = min(max(1, int(k)), self.n)
        if self.k != k:
            # k > n could never be met: every alarm would be silently disabled.
            log.error("confirmation k=%s is invalid for n=%s; using %d", k, n, self.k)
        self.max_gap = max_gap_seconds if max_gap_seconds > 0 else 3.0
        self._obs: deque[tuple[float, bool]] = deque(maxlen=self.n)

    def _expire(self, now: float) -> None:
        if self._obs and now - self._obs[-1][0] > self.max_gap:
            self._obs.clear()

    def _state(self) -> tuple[int, int, bool]:
        hits = sum(1 for _, h in self._obs if h)
        return hits, len(self._obs), hits >= self.k

    def push(self, now: float, hit: bool) -> tuple[int, int, bool]:
        self._expire(now)
        self._obs.append((now, bool(hit)))
        return self._state()

    def peek(self, now: float) -> tuple[int, int, bool]:
        """Current state without a new observation (for a cached value)."""
        self._expire(now)
        return self._state()

    def reset(self) -> None:
        self._obs.clear()


@dataclass
class Vote:
    """One source's opinion about one class on this tick."""

    source: str
    value: float
    threshold: float
    hits: int = 0
    seen: int = 0
    confirmed: bool = False
    #: Why a crossing did not count (e.g. no person), if it did not.
    gated: str | None = None
    #: False when the value is a cached one, not a new model evaluation.
    fresh: bool = True
    #: False for an unusable input (non-finite value, threshold outside (0, 1)).
    valid: bool = True

    def describe(self) -> str:
        text = (f"{self.source} {self.value:.2f}/{self.threshold:.2f} "
                f"({self.hits}/{self.seen} ticks)")
        if not self.fresh:
            text += " (cached)"
        if self.gated:
            text += f" [{self.gated}]"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "value": round(self.value, 4),
                "threshold": round(self.threshold, 4), "hits": self.hits,
                "seen": self.seen, "confirmed": self.confirmed,
                "gated": self.gated, "fresh": self.fresh}


@dataclass
class ClassEvidence:
    """Everything known about one threat class on this tick."""

    name: str
    score: float = 0.0
    confirmed: bool = False
    experimental: bool = False
    votes: list[Vote] = field(default_factory=list)
    reason: str = ""
    #: ``unconfirmed``, ``confirmed`` (one source) or ``corroborated``.
    state: str = "unconfirmed"

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 4), "confirmed": self.confirmed,
                "state": self.state, "experimental": self.experimental,
                "reason": self.reason, "votes": [v.to_dict() for v in self.votes]}


def strength(vote: Vote) -> float:
    """How far over its threshold a vote is: 0 at the threshold, 1 at certainty."""
    if not vote.valid:
        return 0.0
    threshold = min(max(vote.threshold, 1e-3), 1.0 - 1e-3)
    return min(max((vote.value - threshold) / (1.0 - threshold), 0.0), 1.0)


def source_score(vote: Vote, cfg=None) -> float:
    """TCI-scale evidence from one vote.

    Confirmed: ``confirmed_floor`` at the threshold rising linearly to
    ``single_source_cap`` at certainty. Unconfirmed: proportional to how close
    the value is to its threshold, never above ``unconfirmed_ceiling``.
    """
    cfg = cfg or settings.threat
    if not vote.valid:
        return 0.0
    if vote.confirmed:
        return (cfg.confirmed_floor
                + (cfg.single_source_cap - cfg.confirmed_floor) * strength(vote))
    threshold = min(max(vote.threshold, 1e-3), 1.0 - 1e-3)
    return cfg.unconfirmed_ceiling * min(max(vote.value, 0.0) / threshold, 1.0)


def corroborated_score(confirmed: list[Vote], cfg=None) -> float | None:
    """Score for a classifier *and* a detector both confirming, else None.

    Two classifiers (scene and violence) are not independent evidence (D3).
    The weaker side sets the score: ``corroborated_floor`` (L4) at threshold
    rising to ``corroborated_cap``; L5 (0.85) needs the weaker side about 60 %
    of the way from its threshold to certainty.
    """
    cfg = cfg or settings.threat
    classifier = max((strength(v) for v in confirmed if v.source in CLASSIFIER_SOURCES),
                     default=None)
    detector = max((strength(v) for v in confirmed if v.source in DETECTOR_SOURCES),
                   default=None)
    if classifier is None or detector is None:
        return None
    weaker = min(classifier, detector)
    return cfg.corroborated_floor + (cfg.corroborated_cap - cfg.corroborated_floor) * weaker


def combine(scores: list[float]) -> float:
    """Noisy-OR: independent sources reinforce, none can lower another."""
    return 1.0 - math.prod(1.0 - min(max(s, 0.0), 1.0) for s in scores)


def strongest(evidence: dict[str, "ClassEvidence | dict | float"]) -> tuple[str | None, float]:
    """The class with the highest score (ties broken by :data:`CLASS_ORDER`).

    Values may be :class:`ClassEvidence`, its ``to_dict()`` form, or a bare score.
    """
    best, best_score = None, 0.0
    for name in CLASS_ORDER:
        item = evidence.get(name)
        if item is None:
            continue
        if isinstance(item, dict):
            score = float(item["score"])
        else:
            score = float(getattr(item, "score", item))
        if score > best_score:
            best, best_score = name, score
    return best, best_score


class EvidenceAccumulator:
    """Stateful per-stream evidence builder. One per pipeline; reset per clip."""

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or settings.threat
        self._windows: dict[tuple[str, str], ConfirmationWindow] = {}

    def _window(self, cls: str, source: str) -> ConfirmationWindow:
        key = (cls, source)
        if key not in self._windows:
            self._windows[key] = ConfirmationWindow(
                self.cfg.confirm_k, self.cfg.confirm_n, self.cfg.confirm_max_gap_seconds)
        return self._windows[key]

    def _allowed(self, source: str, cls: str) -> bool:
        mode = self.cfg.sources
        if mode == "scene":
            return source in CLASSIFIER_SOURCES
        if mode == "detector":
            # The detector has no assault/robbery head; the classifiers keep
            # voting for those (experimental, capped at L2 anyway).
            return source in DETECTOR_SOURCES or cls in self.cfg.experimental_classes
        return True

    def update(self, now: float, sources: dict[str, dict[str, tuple]],
               person_recent: bool | None) -> dict[str, ClassEvidence]:
        """Fold this tick's votes into confirmed per-class evidence.

        ``person_recent`` is None when the person detector is unavailable: the
        weapon gate is then skipped (and says so) rather than silencing every
        weapon alarm.
        """
        result: dict[str, ClassEvidence] = {}
        for cls in CLASS_ORDER:
            votes: list[Vote] = []
            for source, per_class in sources.items():
                if cls not in per_class or not self._allowed(source, cls):
                    continue
                vote = self._vote(now, cls, source, per_class[cls], person_recent)
                if vote is not None:
                    votes.append(vote)
            result[cls] = self._score(cls, votes)
            if cls == "weapon" and person_recent is None and votes:
                result[cls].reason += " [person gate off: person detector unavailable]"
        return result

    def _vote(self, now: float, cls: str, source: str, entry: tuple,
              person_recent: bool) -> Vote | None:
        value, threshold = float(entry[0]), float(entry[1])
        fresh = bool(entry[2]) if len(entry) > 2 else True
        if not math.isfinite(value) or not (math.isfinite(threshold) and 0.0 < threshold < 1.0):
            what = "invalid value" if not math.isfinite(value) else "invalid threshold"
            return Vote(source, value if math.isfinite(value) else 0.0,
                        threshold if math.isfinite(threshold) else 0.0,
                        gated=what, fresh=fresh, valid=False)
        window = self._window(cls, source)
        gated = None
        if fresh:
            crossing = value >= threshold
            if crossing and cls == "weapon" and person_recent is False:
                crossing, gated = False, "no person in view"
            hits, seen, confirmed = window.push(now, crossing)
        else:
            hits, seen, confirmed = window.peek(now)
            if seen == 0:
                return None             # nothing recent enough to carry
        if confirmed and cls == "weapon" and person_recent is False:
            confirmed, gated = False, "no person in view"
        return Vote(source, value, threshold, hits, seen, confirmed, gated, fresh)

    def _score(self, cls: str, votes: list[Vote]) -> ClassEvidence:
        cfg = self.cfg
        experimental = cls in cfg.experimental_classes
        confirmed = [v for v in votes if v.confirmed]

        corroborated = corroborated_score(confirmed, cfg)
        if corroborated is not None:
            score = corroborated
        else:
            score = combine([source_score(v, cfg) for v in votes])
            score = min(score, cfg.single_source_cap if confirmed else cfg.unconfirmed_ceiling)
        if experimental:
            score = min(score, cfg.experimental_cap if confirmed else cfg.unconfirmed_ceiling)

        state = ("corroborated" if corroborated is not None
                 else "confirmed" if confirmed else "unconfirmed")
        detail = "; ".join(v.describe() for v in votes) or "no source"
        reason = f"{cls} {state}: {detail}"
        if experimental:
            reason += " [experimental: capped at L2]"
        return ClassEvidence(cls, score, bool(confirmed), experimental, votes, reason,
                             state)

    def reset(self) -> None:
        self._windows.clear()


__all__ = ["CLASS_ORDER", "ClassEvidence", "ConfirmationWindow", "EvidenceAccumulator",
           "Vote", "combine", "corroborated_score", "source_score", "strength",
           "strongest"]


# ──────────────────────────────────────────────────────────────────────────────
# Within-clip confirmation (HARDWARE.md T1; docs/7 §2.4 seam, track T-CLIP)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ClipObservation:
    """One source's value for one class on one keyframe of a clip.

    ``group`` says what the value belongs to: a person track
    (``("track", 7)``) or an image region (``("region", 2)``) for a detector
    box, None for a frame-level classifier (the scene model, the violence
    window). ``person_recent`` is the weapon gate at that keyframe (None: the
    person detector is unavailable, so the gate cannot apply).
    """

    source: str
    cls: str
    value: float
    threshold: float
    at: float
    group: Any = None
    person_recent: bool | None = True


@dataclass
class KeyframeVote(Vote):
    """A :class:`Vote` counted in keyframes of one clip instead of ticks."""

    #: Seconds from the first to the last crossing keyframe of ``group``.
    span: float = 0.0
    group: Any = None
    unit: str = "keyframes"

    def describe(self) -> str:
        where = ""
        if isinstance(self.group, tuple) and len(self.group) == 2:
            where = f", {self.group[0]} {self.group[1]}"
        text = (f"{self.source} {self.value:.2f}/{self.threshold:.2f} "
                f"({self.hits}/{self.seen} {self.unit}{where})")
        if self.gated:
            text += f" [{self.gated}]"
        return text

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(span_s=round(self.span, 3), unit=self.unit,
                    group=list(self.group) if isinstance(self.group, tuple) else self.group)
        return data


class ClipConfirmation:
    """Per-class evidence for one clip, confirmed inside the clip.

    The tick loop confirms a source on ``confirm_k`` of its last
    ``confirm_n`` ticks. A 4-second clip already holds time, so here a source
    is confirmed for a class when it crosses that class's threshold on
    ``confirm_keyframes`` distinct keyframes **of one group**: one person
    track for a weapon box, one image region for a fire box, the frame for a
    classifier. Two pistols glimpsed on two different people, or two flames in
    two corners, do not confirm each other. The weapon gate applies per
    keyframe (a crossing counts only with a person seen recently).

    The result is the same ``{class: ClassEvidence}`` an
    :class:`EvidenceAccumulator` produces, scored by the same rules - one
    confirmed source in the L3 band, a classifier and a detector both
    confirmed in L4/L5, experimental classes capped at L2 - so ``decide`` and
    the TCI are unchanged. Overlapping clips are not independent; the only
    cross-clip rule is the level policy's dwell.
    """

    def __init__(self, confirm_keyframes: int | None = None, cfg=None) -> None:
        self.cfg = cfg or settings.threat
        k = settings.clip.confirm_keyframes if confirm_keyframes is None else confirm_keyframes
        self.k = max(1, int(k))
        self._scorer = EvidenceAccumulator(self.cfg)

    def update(self, observations) -> dict[str, ClassEvidence]:
        by_source: dict[tuple[str, str], list[ClipObservation]] = {}
        for obs in observations:
            by_source.setdefault((obs.cls, obs.source), []).append(obs)
        result: dict[str, ClassEvidence] = {}
        for cls in CLASS_ORDER:
            votes: list[Vote] = []
            gate_off = False
            for (c, source), items in sorted(by_source.items()):
                if c != cls or not self._scorer._allowed(source, cls):
                    continue
                votes.append(self.vote(cls, source, items))
                gate_off = gate_off or (cls == "weapon" and all(
                    o.person_recent is None for o in items))
            result[cls] = self._scorer._score(cls, votes)
            if gate_off:
                result[cls].reason += " [person gate off: person detector unavailable]"
        return result

    def vote(self, cls: str, source: str, items: list[ClipObservation]) -> KeyframeVote:
        """One source's vote for one class over the clip's keyframes."""
        valid = [o for o in items if math.isfinite(o.value) and math.isfinite(o.threshold)
                 and 0.0 < o.threshold < 1.0]
        if not valid:
            first = items[0]
            what = ("invalid value" if not math.isfinite(first.value)
                    else "invalid threshold")
            return KeyframeVote(source, first.value if math.isfinite(first.value) else 0.0,
                                first.threshold if math.isfinite(first.threshold) else 0.0,
                                gated=what, valid=False)
        seen = len({o.at for o in valid})
        crossings: dict[Any, dict[float, ClipObservation]] = {}
        gated = None
        for obs in valid:
            if obs.value < obs.threshold:
                continue
            if cls == "weapon" and obs.person_recent is False:
                gated = "no person in view"
                continue
            slots = crossings.setdefault(obs.group, {})
            if obs.at not in slots or obs.value > slots[obs.at].value:
                slots[obs.at] = obs
        best_group, best_slots = None, {}
        for group, slots in crossings.items():
            key = (len(slots), max(o.value for o in slots.values()))
            if not best_slots or key > (len(best_slots),
                                        max(o.value for o in best_slots.values())):
                best_group, best_slots = group, slots
        hits = len(best_slots)
        confirmed = hits >= self.k
        if best_slots:
            top = max(best_slots.values(), key=lambda o: o.value)
            times = sorted(best_slots)
            span = times[-1] - times[0]
        else:
            top = max(valid, key=lambda o: o.value)
            span = 0.0
        return KeyframeVote(source, top.value, top.threshold, hits=hits, seen=seen,
                            confirmed=confirmed, gated=None if confirmed else gated,
                            fresh=True, span=span, group=best_group)


__all__ += ["ClipConfirmation", "ClipObservation", "KeyframeVote"]
