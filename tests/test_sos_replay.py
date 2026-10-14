"""The SOS engine against the decision layer (track T-SOS): the pipeline hook
opens one request and takes over the incident's messages; the evaluation
harness opens none even with the real service installed; and replays of
home-mode decisions — the first live session's log when it is available
(``var/`` is private and not in git), and a synthetic stream shaped like it —
open no request at all.
"""

from __future__ import annotations

import os
import queue
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sentrix import mode
from sentrix.actor import Actor
from sentrix.config import settings
from sentrix.paths import ROOT_DIR
from sentrix.pipeline.escalation import plan_for
from sentrix.sos import LevelChange, NullSosService, SosTrigger
from sentrix.sos.policy import Reach
from sentrix.storage import requests

from test_sos_engine import Clock, FakePipeline, make_profile  # noqa: E402

LIVE_LOG_NAME = "events_20260930_232502_c27258a25457.jsonl"


def make_engine(clock, state=None):
    from sentrix.pipeline.state import SystemState
    from sentrix.sos import pins
    from sentrix.sos.service import SosEngine
    engine = SosEngine(clock=clock, state=state or SystemState(), channels=lambda: None,
                       pipeline=lambda: FakePipeline(), profile=lambda: make_profile(),
                       publish=lambda *a, **k: None,
                       reach=lambda: Reach(online=True, standalone=False, remote_prompt=True),
                       limiter=pins.PinLimiter(5, 600, clock=clock))
    engine._sweeper.start = lambda: None
    engine.start()
    return engine


# ── the pipeline hook ─────────────────────────────────────────────────────────
def hook_pipeline(sos):
    """The pipeline's ``_act``/``_level_changed`` on a bare object (no models)."""
    from sentrix.pipeline.engine import Pipeline
    from sentrix.pipeline.incidents import IncidentManager
    fake = Pipeline.__new__(Pipeline)
    fake.cfg = settings
    fake.incidents = IncidentManager()
    fake.sos = sos
    fake.session_id = "sess-hook"
    fake._committed_level = 1
    fake._tasks = queue.Queue(maxsize=64)
    fake.submitted = []
    fake._submit = lambda func, *a, **k: fake.submitted.append(func.__name__)
    fake._evidence_context = lambda result: {}
    fake.siren = SimpleNamespace(activate=lambda: fake.submitted.append("siren"))
    return fake


def result_of(kind, level, state="confirmed"):
    extras = {"evidence": {kind: {"state": state}}, "detections": {"all": []}}
    return SimpleNamespace(incident_type=kind, tci=0.5 if level == 3 else 0.8,
                           reason=f"{kind} {state}", features=SimpleNamespace(extras=extras))


def drain(fake):
    while not fake._tasks.empty():
        func, args, kwargs = fake._tasks.get_nowait()
        func(*args, **kwargs)


class TestPipelineHook:
    def test_one_request_per_incident_and_sos_owns_its_messages(self, temp_db):
        from sentrix.storage import database
        clock = Clock()
        engine = make_engine(clock)
        try:
            fake = hook_pipeline(engine)
            incident, _ = fake.incidents.update(3, 0.5, "weapon", now=0.0, features={},
                                                reason="weapon confirmed")
            database.save_incident(incident, "sess-hook", ongoing=True)
            frame = np.zeros((4, 4, 3), np.uint8)
            for _ in range(3):
                fake._act(incident, plan_for(3), frame, result_of("weapon", 3), 3,
                          "ELEVATED", 0.0)
            drain(fake)
            engine.work()
            assert len(requests.list_requests(5)) == 1
            req = requests.list_requests(1)[0]
            assert req["state"] == "PENDING" and req["incident_id"] == incident.id
            # L4 and L5: the legacy SMS, package and call are the request's now.
            for level in (4, 5):
                change = LevelChange(level=level, previous_level=level - 1,
                                     incident_id=incident.id, incident_type="weapon",
                                     evidence_state="corroborated", tci=0.9,
                                     at_wall=clock(), at_mono=0.0)
                fake._act(incident, plan_for(level),
                          frame, result_of("weapon", level, "corroborated"), level,
                          "HIGH", 1.0)
                engine.on_level(change)
            engine.work()
            assert "send_sms" not in fake.submitted and "make_call" not in fake.submitted
            assert "_create_dispatch" not in fake.submitted
            assert "siren" in fake.submitted, "the siren stays"
            req = requests.get_request(req["id"])
            assert req["peak_level"] == 5 and req["urgency"] == "HIGH"
            assert len(requests.list_requests(5)) == 1
        finally:
            engine.stop()

    def test_experimental_classes_are_never_claimed(self, temp_db):
        engine = make_engine(Clock())
        try:
            fake = hook_pipeline(engine)
            incident, _ = fake.incidents.update(3, 0.5, "assault", now=0.0)
            fake._act(incident, plan_for(3), np.zeros((4, 4, 3), np.uint8),
                      result_of("assault", 3), 3, "ELEVATED", 0.0)
            assert "sos" not in incident.actions and fake._tasks.empty()
        finally:
            engine.stop()


# ── the evaluation harness ────────────────────────────────────────────────────
class _Light:
    """Stands in for a perception engine: no model is loaded."""

    available = True
    weights_name = "light"
    backend = "light"
    name = "light"

    def __init__(self, *args, **kwargs):
        pass

    def reset(self, *args, **kwargs):
        pass

    def reset_tracks(self):
        pass


class _Events:
    def __init__(self, *args, **kwargs):
        self.session_id = "eval-session"
        self.rows = 0

    def write(self, *args, **kwargs):
        self.rows += 1

    def audit(self, *args, **kwargs):
        pass

    def close(self):
        pass


@pytest.fixture
def light_pipelines(monkeypatch):
    """``Pipeline`` builds with light engines and an in-memory event log."""
    from sentrix.pipeline import engine as engine_module
    for name in ("Detector", "MotionEngine", "SceneEngine", "BehaviourEngine",
                 "IdentityEngine", "TrackIdentity", "ReIDEngine"):
        monkeypatch.setattr(engine_module, name, _Light)
    monkeypatch.setattr(engine_module, "EventLog", _Events)
    monkeypatch.setattr(engine_module, "build_siren",
                        lambda: SimpleNamespace(name="light", activate=lambda: None))
    built = []
    yield built
    for pipeline in built:
        pipeline._stop.set()


def weapon_stream(seconds=8.0, hz=2.0):
    """Feature vectors of a sustained, confirmed single-source weapon."""
    from sentrix.fusion.evidence import EvidenceAccumulator
    from sentrix.fusion.features import FeatureVector
    acc = EvidenceAccumulator()
    for i in range(int(seconds * hz)):
        now = i / hz
        evidence = acc.update(now, {"scene": {"weapon": (0.9, 0.5, True)},
                                    "detector": {"weapon": (0.0, 0.5, True)}},
                              person_recent=True)
        yield now, FeatureVector(
            weapon=evidence["weapon"].score, fire=evidence["fire"].score,
            person_present=True, person_count=1,
            extras={"evidence": {c: e.to_dict() for c, e in evidence.items()},
                    "detections": {"all": [], "persons": []}})


class TestEvaluationOpensNone:
    def test_eval_harness_with_the_real_service_installed(self, temp_db, light_pipelines):
        from sentrix import services
        from sentrix.evaluation.harness import build_eval_pipeline
        engine = make_engine(Clock())
        services.registry.register(engine)                 # registered, not running
        try:
            pipeline = build_eval_pipeline()
            light_pipelines.append(pipeline)
            assert isinstance(pipeline.sos, NullSosService)
            frame = np.zeros((8, 8, 3), np.uint8)
            levels = [pipeline.decide(frame, features, now)["level"]
                      for now, features in weapon_stream()]
            assert max(levels) >= 3, "the stream does reach a confirmed L3"
            engine.work()
            assert requests.list_requests(5) == [], "measurement never opens a request"
        finally:
            engine.stop()

    def test_the_same_stream_opens_one_request_on_a_live_pipeline(self, temp_db,
                                                                  light_pipelines):
        """Positive control: the decision stream above is an SOS case."""
        from sentrix.pipeline.engine import Pipeline
        from sentrix.storage import database
        engine = make_engine(Clock())
        try:
            pipeline = Pipeline(source=SimpleNamespace(online=True, release=lambda: None),
                                enable_audio=False, event_source="test")
            light_pipelines.append(pipeline)
            pipeline._capture_snapshot = lambda *a, **k: None      # nothing to disk
            pipeline._capture_evidence = lambda *a, **k: None
            pipeline.sos = engine
            frame = np.zeros((8, 8, 3), np.uint8)
            for now, features in weapon_stream():
                pipeline.decide(frame, features, now)
            pipeline._tasks.join()
            engine.work()
            found = requests.list_requests(5)
            assert len(found) == 1 and found[0]["incident_type"] == "weapon"
            assert database.recent_incidents(5)[0]["request_id"] == found[0]["id"]
        finally:
            engine.stop()


# ── replays of home-mode decisions ────────────────────────────────────────────
def _tool():
    sys.path.insert(0, str(ROOT_DIR / "tools"))
    try:
        import replay_decisions
    finally:
        sys.path.pop(0)
    return replay_decisions


def replay_into_engine(rows, *, armed: bool, tool) -> tuple[SosEngineStats, list]:
    """Re-run the current decision layer on logged ticks (as
    ``tools/replay_decisions.py`` does) and drive the SOS hook exactly as
    ``Pipeline._act`` / ``_level_changed`` do."""
    import dataclasses

    from sentrix.fusion.evidence import EvidenceAccumulator
    from sentrix.fusion.features import FeatureVector
    from sentrix.fusion.policy import LevelPolicy
    from sentrix.fusion.tci import TCIEngine
    from sentrix.pipeline.incidents import IncidentManager
    from sentrix.pipeline.state import SystemState
    from sentrix.storage import database

    threat = dataclasses.replace(settings.threat, armed=armed)
    acc, tci = EvidenceAccumulator(threat), TCIEngine()
    tci.threat = threat
    policy, incidents = LevelPolicy(), IncidentManager()
    clock, state = Clock(0.0), SystemState()
    engine = make_engine(clock, state)
    if armed:
        mode.set_armed(True, by="replay")
    committed, last_person, levels = 1, None, []
    try:
        for row in rows:
            t, sources, context = tool.tick_inputs(row, threat.scene_threshold_floor,
                                                   settings.detection)
            clock.now = t
            person = bool(context.get("person_present"))
            if person:
                last_person = t
            person_recent = (last_person is not None
                             and t - last_person <= threat.person_memory_seconds)
            if "person_recent" in context:
                person_recent = context["person_recent"]
            evidence = acc.update(t, sources, person_recent=person_recent)
            features = FeatureVector(
                weapon=evidence["weapon"].score, fire=evidence["fire"].score,
                person_present=person, authorized=bool(context.get("authorized")),
                identity_degraded=bool(context.get("identity_degraded")),
                extras={"evidence": {c: e.to_dict() for c, e in evidence.items()}})
            result = tci.compute(features, t)
            level = policy.update(result.raw_tci, t).level
            incident, closed = incidents.update(level, result.tci, result.incident_type, t,
                                                features=features.model_features(),
                                                reason=result.reason)
            if incident is not None and incident.saved_at is None:
                incident.saved_at = t
                database.save_incident(incident, "replay", ongoing=True)
            state.update(authorized=features.authorized, person_count=int(person),
                         identity_degraded=features.identity_degraded)
            votes = features.extras["evidence"]
            if level != committed:
                engine.on_level(LevelChange(
                    level=level, previous_level=committed,
                    incident_id=incident.id if incident else None,
                    incident_type=result.incident_type,
                    evidence_state=(votes.get(result.incident_type) or {}).get("state", "none"),
                    tci=result.tci, at_wall=t, at_mono=t))
                committed = level
            if (incident is not None and plan_for(level).sos
                    and "sos" not in incident.actions
                    and engine.wants(result.incident_type, level)):
                incident.actions.add("sos")
                engine.trigger(SosTrigger(
                    incident_id=incident.id, level=level,
                    incident_type=result.incident_type,
                    evidence_state=(votes.get(result.incident_type) or {}).get("state", "none"),
                    tci=result.tci, reason=result.reason, session_id="replay",
                    armed=armed, at_wall=t, at_mono=t))
            levels.append(level)
            engine.work()
        clock.now += 60
        engine.work()
        stats = SosEngineStats(requests=requests.list_requests(100),
                               counters=dict(engine.counters),
                               held=engine.health()["held_intrusions"])
        return stats, levels
    finally:
        engine.stop()
        mode.clear_override()


class SosEngineStats(SimpleNamespace):
    pass


def synthetic_home_session(minutes=12.0, hz=2.0, seed=7):
    """Schema-3 rows shaped like the first live session (CLAUDE.md §5): an
    out-of-distribution room where the scene classifier's p(weapon) hovers
    around 0.3 and pokes over the old 0.35 threshold one tick at a time,
    team members (unknown faces) coming and going, no detector boxes."""
    rng = random.Random(seed)
    rows, present = [], False
    floors = settings.threat.scene_threshold_floor
    for i in range(int(minutes * 60 * hz)):
        t = 1000.0 + i / hz
        if rng.random() < 0.01:
            present = not present
        weapon = min(0.49, max(0.0, rng.gauss(0.30, 0.05)))
        if rng.random() < 0.08:
            weapon = rng.uniform(0.35, 0.49)      # single-tick flickers under the floor
        fire = max(0.0, rng.gauss(0.05, 0.03))
        rows.append({"schema": 3, "ts": t, "latency_ms": 0.0, "level": 1, "features": {},
                     "votes": {"scene": {"weapon": [weapon, floors["weapon"], True],
                                         "fire": [fire, floors["fire"], True]},
                               "detector": {"weapon": [0.0, settings.detection.weapon_conf, True],
                                            "fire": [0.0, settings.detection.fire_conf, True]}},
                     "context": {"person_present": present, "authorized": False,
                                 "identity_degraded": True}})
    return rows


class TestHomeModeReplays:
    def test_a_live_like_home_session_opens_no_request(self, temp_db):
        stats, levels = replay_into_engine(synthetic_home_session(), armed=False, tool=_tool())
        assert stats.requests == [] and stats.counters["triggers"] == 0
        assert max(levels) <= 2

    def test_the_first_live_session_in_home_mode_opens_no_request(self, temp_db):
        """CLAUDE.md M1 acceptance: L3+ onsets 0 in home mode. The log is
        private runtime output (not in git); set SENTRIX_LIVE_SESSION_LOG to
        replay it from another checkout."""
        path = Path(os.getenv("SENTRIX_LIVE_SESSION_LOG", "")
                    or ROOT_DIR / "var" / "events" / LIVE_LOG_NAME)
        if not path.is_file():
            pytest.skip(f"live session log not present ({path.name})")
        tool = _tool()
        rows = tool.load(path)
        stats, levels = replay_into_engine(rows, armed=False, tool=tool)
        assert stats.requests == [], [r["incident_type"] for r in stats.requests]
        assert stats.counters["triggers"] == 0 and max(levels) <= 2
        # Armed: only intrusions that outlive the 30 s entry delay, nothing else.
        armed, _ = replay_into_engine(rows, armed=True, tool=tool)
        assert all(r["incident_type"] == "intrusion" for r in armed.requests)
        assert len(armed.requests) <= armed.counters["held"]


def test_replay_tools_keep_the_null_service():
    for tool in ("replay.py", "bench_live.py"):
        source = (ROOT_DIR / "tools" / tool).read_text(encoding="utf-8")
        assert "assert_measurement_safe(pipeline)" in source
    harness = (ROOT_DIR / "sentrix" / "evaluation" / "harness.py").read_text(encoding="utf-8")
    assert "pipeline.sos = NullSosService()" in harness
    assert Actor.system().ref == "system"
