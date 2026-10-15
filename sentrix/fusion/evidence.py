"""Per-class threat evidence: temporal confirmation and multimodal fusion.

Why this exists
---------------
The first live run (CLAUDE.md §5) produced ~171 false weapon alarms per hour.
Every one came from a single tick of the scene classifier a hair over its
threshold, in a room it had never seen; a single frame was enough because the
old TCI engine turned any crossing into a hard override. This module is the
only place threat evidence is formed. Before any source may count:

1. **Confirmation counted in observations.** A source's crossings for a class
   count only once they occur on ``confirm_k`` of the last ``confirm_n``
   fresh evaluations (default 2 of 3). Counting observations rather than
   seconds keeps the rule honest at 0.5 Hz, where a 0.5 s window held a
   single frame.
2. **Gates from context.** A weapon needs a person seen in the last
   ``person_memory_seconds``. A fight needs ``fight_min_persons`` people in
   view (when the person count is known).

How the modalities combine (D15)
--------------------------------
Every source belongs to one **modality**: the YOLO heads and the clip
engine's fast lane are ``video_detector``; the scene classifier and the
violence model are ``video_classifier`` (two classifiers looking at the same
pixels are not independent, D3); the sound model is ``audio`` - and audio
votes exist only when the audio window describes this frame (time-aligned,
``perception/audio.py``), otherwise audio is *unavailable*, never "calm".

For the alarm classes (weapon, fire):

* nothing confirmed: under the L2 band (``unconfirmed_ceiling``) - shown,
  never acted on;
* audio confirmed with no visual source: L2 at most (``audio_only_cap``);
* one visual modality confirmed: the L3 band (``confirmed_floor`` ..
  ``single_source_cap``) - snapshot and evidence, no SMS or siren;
* two independent modalities confirmed (detector + scene, or video + aligned
  audio): L4 at threshold rising to L5, the weaker of the two setting the
  value (``corroborated_floor`` .. ``corroborated_cap``);
* all three confirmed: at least L5 (``three_modality_floor``).

For a fight (``assault``, experimental, D1): capped at L2 (``experimental_cap``)
unless the video fight **and** aligned aggression audio are both confirmed,
with ``fight_min_persons`` people and motion over ``fight_motion_floor``: then
the L3 band (state ``corroborated``: two modalities). L4 needs, on top, a
second visual *modality* (a detector vote; scene + violence are one), and
``fight_max_level`` is a hard cap (the score stays under the next band).
Aligned laughter or very calm talk (``audio_benign``) with no aggression is
banter: the fight stays at L2 - logged and shown, labelled banter, never lower.
Aggression and banter in the same audio are conflicting: L2. TV, music or
radio in the window make the audio neutral (no audio vote at all,
``perception/audio.audio_votes``). Robbery stays experimental (L2).

An audio window is a new observation only if it does not overlap the last
one counted (the pipeline's ``fresh`` flag): with 4 s windows every 1 s, one
impulse would otherwise be "confirmed" by three overlapping windows. Audio
confirmation windows tolerate the longer gap between non-overlapping windows
(``audio_confirm_max_gap_seconds``).

Identity never relaxes threat evidence: frame-level identity cannot tell a
resident from a stranger beside them (M4 is per-track, but no rule uses it to
lower a weapon). Unknown people are a separate, L2-only rule in the TCI engine.

Sources
-------
Each tick the pipeline hands in ``sources``: ``{source_name: {class: (value,
threshold[, fresh])}}``. Only *fresh* values - a model evaluation made on this
tick (for audio: a new window) - are pushed into the confirmation window; a
cached value is carried with the window's state and never counts as a new
observation, so one evaluation cannot confirm itself. A value that is not
finite, or a threshold outside (0, 1), is recorded as ``gated`` and
contributes nothing. ``audio_benign`` votes *against* a fight.

The resulting per-class ``score`` is on the TCI scale and is what the TCI engine
takes the maximum of — so any event row can be re-derived by hand from the votes
it records (and ``modalities`` in each class's record says which counted).
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
DETECTOR_SOURCES = frozenset({"detector", "fastlane"})
AUDIO_SOURCES = frozenset({"audio", "audio_aggression", "audio_benign"})
#: Sources that argue *against* a class (banter lowers a fight). They confirm
#: on their newest fresh observation: lowering an experimental class needs
#: less evidence than raising one.
AGAINST_SOURCES = frozenset({"audio_benign"})
#: Sources that may corroborate a class but never stand alone for it (a
#: scream next to a visible gun corroborates the gun; a scream alone is not
#: a weapon).
CORROBORATE_ONLY = frozenset({"audio_aggression"})

VIDEO_DETECTOR, VIDEO_CLASSIFIER, AUDIO = "video_detector", "video_classifier", "audio"
VISUAL_MODALITIES = frozenset({VIDEO_DETECTOR, VIDEO_CLASSIFIER})
#: Display names for the dashboard's contributions list.
MODALITY_NAMES = {VIDEO_DETECTOR: "video detector", VIDEO_CLASSIFIER: "scene/action model",
                  AUDIO: "audio"}


def modality_of(source: str) -> str:
    if source in DETECTOR_SOURCES:
        return VIDEO_DETECTOR
    if source in AUDIO_SOURCES:
        return AUDIO
    return VIDEO_CLASSIFIER


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

    def latest(self) -> bool:
        """Whether the newest observation was a crossing."""
        return bool(self._obs) and self._obs[-1][1]

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

    @property
    def modality(self) -> str:
        return modality_of(self.source)

    @property
    def role(self) -> str:
        """``for`` (raises the class) or ``against`` (banter lowers a fight)."""
        return "against" if self.source in AGAINST_SOURCES else "for"

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
                "gated": self.gated, "fresh": self.fresh,
                "modality": self.modality, "role": self.role}


@dataclass
class ClassEvidence:
    """Everything known about one threat class on this tick."""

    name: str
    score: float = 0.0
    confirmed: bool = False
    experimental: bool = False
    votes: list[Vote] = field(default_factory=list)
    reason: str = ""
    #: ``unconfirmed``, ``confirmed`` (one modality) or ``corroborated`` (two+).
    state: str = "unconfirmed"
    #: Confirmed modalities -> strength of their strongest confirmed vote.
    modalities: dict = field(default_factory=dict)
    #: The rule that set the score (``single``, ``corroborated``, ``banter``...).
    rule: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 4), "confirmed": self.confirmed,
                "state": self.state, "experimental": self.experimental,
                "reason": self.reason, "rule": self.rule,
                "modalities": {m: round(s, 4) for m, s in self.modalities.items()},
                "votes": [v.to_dict() for v in self.votes]}


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


def confirmed_modalities(votes: list[Vote]) -> dict[str, float]:
    """Modality -> the strength of its strongest confirmed supporting vote."""
    out: dict[str, float] = {}
    for vote in votes:
        if vote.confirmed and vote.valid and vote.role == "for":
            out[vote.modality] = max(out.get(vote.modality, 0.0), strength(vote))
    return out


def corroborated_score(confirmed: list[Vote], cfg=None) -> float | None:
    """Score for two or more independent modalities confirming, else None.

    The second-strongest modality (the weaker side of the best pair) sets the
    score: ``corroborated_floor`` (L4) at threshold rising to
    ``corroborated_cap``; L5 (0.85) needs it about 60 % of the way from its
    threshold to certainty. Two classifiers (scene and violence) are one
    modality, not corroboration (D3). Audio needs a visual partner: two audio
    votes never corroborate each other. Three modalities: at least
    ``three_modality_floor`` (L5).
    """
    cfg = cfg or settings.threat
    modalities = confirmed_modalities(confirmed)
    if len(modalities) < 2 or not (set(modalities) & VISUAL_MODALITIES):
        return None
    ranked = sorted(modalities.values(), reverse=True)
    score = cfg.corroborated_floor + (cfg.corroborated_cap - cfg.corroborated_floor) * ranked[1]
    if len(modalities) >= 3 and three_modality_ok(modalities, cfg):
        score = max(score, cfg.three_modality_floor)
    return min(score, cfg.corroborated_cap)


def three_modality_ok(modalities: dict[str, float], cfg=None) -> bool:
    """The L5 floor for three modalities needs the audio among them to be
    more than a whisper over its bar (``three_modality_audio_min_strength``):
    replica-gun handling scores "Cap gun" 0.2-0.8, and one such sound must not
    turn a corroborated L4 into a siren."""
    cfg = cfg or settings.threat
    audio = modalities.get(AUDIO)
    return audio is None or audio >= float(getattr(cfg, "three_modality_audio_min_strength",
                                                   0.3))


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
            gap = self.cfg.confirm_max_gap_seconds
            if source in AUDIO_SOURCES:
                # Counted audio windows do not overlap: 4 s apart at best.
                gap = max(gap, float(getattr(self.cfg, "audio_confirm_max_gap_seconds", 6.0)))
            self._windows[key] = ConfirmationWindow(
                self.cfg.confirm_k, self.cfg.confirm_n, gap)
        return self._windows[key]

    def _allowed(self, source: str, cls: str) -> bool:
        if source in AUDIO_SOURCES:
            return bool(getattr(self.cfg, "audio_votes", True))
        mode = self.cfg.sources
        if mode == "scene":
            return source in CLASSIFIER_SOURCES
        if mode == "detector":
            # The detector has no assault/robbery head; the classifiers keep
            # voting for those (experimental, capped at L2 anyway).
            return source in DETECTOR_SOURCES or cls in self.cfg.experimental_classes
        return True

    def update(self, now: float, sources: dict[str, dict[str, tuple]],
               person_recent: bool | None,
               context: dict | None = None) -> dict[str, ClassEvidence]:
        """Fold this tick's votes into confirmed per-class evidence.

        ``person_recent`` is None when the person detector is unavailable: the
        weapon gate is then skipped (and says so) rather than silencing every
        weapon alarm. ``context`` carries what is not a vote but conditions
        one: ``persons`` (people in view recently, None if unknown) and
        ``motion`` (0..1, None if unknown) for the fight rule.
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
            result[cls] = self._score(cls, votes, context)
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
        if source in AGAINST_SOURCES:
            confirmed = window.latest()
        if confirmed and cls == "weapon" and person_recent is False:
            confirmed, gated = False, "no person in view"
        return Vote(source, value, threshold, hits, seen, confirmed, gated, fresh)

    # ── scoring ───────────────────────────────────────────────────────────────
    def _gate(self, cls: str, votes: list[Vote], context: dict | None) -> None:
        """Context gates that apply after confirmation (the fight's people)."""
        if cls != "assault" or not context:
            return
        persons = context.get("persons")
        need = int(getattr(self.cfg, "fight_min_persons", 2))
        if persons is not None and persons < need:
            for vote in votes:
                if vote.role == "for" and vote.confirmed:
                    vote.confirmed = False
                    vote.gated = f"fewer than {need} people in view"

    def _score(self, cls: str, votes: list[Vote],
               context: dict | None = None) -> ClassEvidence:
        cfg = self.cfg
        self._gate(cls, votes, context)
        experimental = cls in cfg.experimental_classes
        support = [v for v in votes if v.role == "for"]
        against = [v for v in votes if v.role == "against"]
        modalities = confirmed_modalities(support)
        visual = {m: s for m, s in modalities.items() if m in VISUAL_MODALITIES}
        base = combine([source_score(v, cfg) for v in support])
        notes: list[str] = []

        if cls == "assault" and experimental:
            score, state, rule = self._fight(support, against, modalities, visual, base,
                                             context, notes)
        elif experimental:
            score = min(base, cfg.experimental_cap if modalities else cfg.unconfirmed_ceiling)
            state = "confirmed" if modalities else "unconfirmed"
            rule = "experimental" if modalities else "unconfirmed"
            notes.append("experimental: capped at L2")
        else:
            corroborated = corroborated_score(support, cfg)
            if corroborated is not None:
                score, state, rule = corroborated, "corroborated", (
                    "three modalities" if len(modalities) >= 3 else "corroborated")
            elif visual:
                score, state, rule = min(base, cfg.single_source_cap), "confirmed", "single"
            elif confirmed_modalities([v for v in support
                                       if v.source not in CORROBORATE_ONLY]):  # audio only
                score, state, rule = (min(base, getattr(cfg, "audio_only_cap", 0.35)),
                                      "confirmed", "audio only")
                notes.append("audio alone: capped at L2")
            elif modalities:
                score, state, rule = (min(base, cfg.unconfirmed_ceiling), "unconfirmed",
                                      "corroboration only")
                notes.append("aggression audio only corroborates a visible weapon")
            else:
                score, state, rule = min(base, cfg.unconfirmed_ceiling), "unconfirmed", \
                    "unconfirmed"

        detail = "; ".join(v.describe() for v in votes) or "no source"
        reason = f"{cls} {state}: {detail}"
        if notes:
            reason += "".join(f" [{n}]" for n in notes)
        return ClassEvidence(cls, score, state != "unconfirmed", experimental, votes,
                             reason, state, modalities, rule)

    def _fight(self, support, against, modalities, visual, base, context, notes):
        """The fight rule (D15): video and aligned aggression audio must agree
        to leave L2; banter (laughter, very calm talk) keeps it at L2."""
        score, state, rule = self._fight_rule(support, against, modalities, visual, base,
                                              context, notes)
        # fight_max_level is a cap: the score stays under the next band.
        max_level = int(getattr(self.cfg, "fight_max_level", 3))
        thresholds = settings.policy.thresholds
        if 2 <= max_level <= len(thresholds):
            score = min(score, thresholds[max_level - 1] - 0.01)
        return score, state, rule

    def _fight_rule(self, support, against, modalities, visual, base, context, notes):
        cfg = self.cfg
        benign = any(v.confirmed and v.valid for v in against)
        aggression = AUDIO in modalities
        context = context or {}
        persons, motion = context.get("persons"), context.get("motion")
        need = int(getattr(cfg, "fight_min_persons", 2))
        people_ok = persons is not None and persons >= need
        motion_ok = motion is not None and motion >= float(getattr(cfg, "fight_motion_floor", 0.0))
        max_level = int(getattr(cfg, "fight_max_level", 3))

        if benign and visual:
            # Banter, or banter mixed with aggression: never above L2, never
            # below it either (a fight with laughter is still shown).
            notes.append("mixed audio: aggression and laughter/calm talk: held at L2"
                         if aggression else
                         "banter: aligned laughter/calm talk, no aggression: held at L2")
            return min(base, cfg.experimental_cap), "confirmed", (
                "mixed audio" if aggression else "banter")
        if visual and aggression and max_level >= 3:
            if not people_ok or not motion_ok:
                why = (f"needs {need}+ people" if not people_ok else "needs motion")
                notes.append(f"video + aggression audio, but {why}: capped at L2")
                return min(base, cfg.experimental_cap), "confirmed", "experimental"
            weaker = min(max(visual.values()), modalities[AUDIO])
            if max_level >= 4 and len(visual) >= 2:
                notes.append("fight: two visual modalities and aggression audio")
                return (cfg.corroborated_floor
                        + (cfg.corroborated_cap - cfg.corroborated_floor) * weaker,
                        "corroborated", "fight corroborated L4")
            notes.append("fight: video and aligned aggression audio agree (L3; unvalidated)")
            return (cfg.confirmed_floor + (cfg.single_source_cap - cfg.confirmed_floor) * weaker,
                    "corroborated", "fight corroborated")
        if benign:
            notes.append("banter heard (laughter/calm talk)")
        if modalities:
            notes.append("experimental: capped at L2 (no aligned aggression audio)"
                         if visual else "audio alone: capped at L2")
            return min(base, cfg.experimental_cap), "confirmed", "experimental"
        return min(base, cfg.unconfirmed_ceiling), "unconfirmed", "unconfirmed"

    def reset(self) -> None:
        self._windows.clear()


__all__ = ["AUDIO", "AUDIO_SOURCES", "CLASS_ORDER", "ClassEvidence", "ConfirmationWindow",
           "EvidenceAccumulator", "MODALITY_NAMES", "VIDEO_CLASSIFIER", "VIDEO_DETECTOR",
           "Vote", "combine", "confirmed_modalities", "corroborated_score", "modality_of",
           "three_modality_ok",
           "source_score", "strength", "strongest"]


# ──────────────────────────────────────────────────────────────────────────────
# Within-clip confirmation (docs/8_RASPBERRY_PI5_INTEGRATION.md T1; docs/7 §2.4 seam, track T-CLIP)
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
        #: Non-overlapping audio parts a source must cross on (the clip's halves).
        self.audio_k = 2
        self._scorer = EvidenceAccumulator(self.cfg)

    def update(self, observations, context: dict | None = None) -> dict[str, ClassEvidence]:
        """``context``: as for :meth:`EvidenceAccumulator.update` (people and
        motion over the clip). Audio observations (the sound model over the
        clip's two non-overlapping halves, so aligned by construction) confirm
        when both halves cross."""
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
            result[cls] = self._scorer._score(cls, votes, context)
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
        # Audio parts never overlap (two halves of the clip): a source needs
        # both, so one impulse cannot confirm itself (review F4); banter
        # (an "against" source) lowers on one, as in the tick loop.
        need = (1 if source in AGAINST_SOURCES
                else self.audio_k if source in AUDIO_SOURCES else self.k)
        confirmed = hits >= need
        if best_slots:
            top = max(best_slots.values(), key=lambda o: o.value)
            times = sorted(best_slots)
            span = times[-1] - times[0]
            if source in AUDIO_SOURCES:
                # ClipObservation.group holds each audio part's length.
                span += float(top.group) if isinstance(top.group, (int, float)) else 0.0
        else:
            top = max(valid, key=lambda o: o.value)
            span = 0.0
        return KeyframeVote(source, top.value, top.threshold, hits=hits, seen=seen,
                            confirmed=confirmed, gated=None if confirmed else gated,
                            fresh=True, span=span, group=best_group,
                            unit="windows" if source in AUDIO_SOURCES else "keyframes")


__all__ += ["ClipConfirmation", "ClipObservation", "KeyframeVote"]
