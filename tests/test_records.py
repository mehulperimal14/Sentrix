"""Records a reviewer can trust (CLAUDE.md M3).

The first live test's incident rows mixed ticks (type from the first frame,
reason from the peak, authorisation hard-coded), lost the open incident on a
hard kill, and the event log lost up to 49 rows. These tests pin each fix. The
database tests run on a temporary SQLite file, never on ``var/sentrix.db``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sentrix.config import settings
from sentrix.fusion.policy import LevelState
from sentrix.pipeline.engine import Pipeline
from sentrix.pipeline.events import EventLog
from sentrix.pipeline.incidents import IncidentManager
from sentrix.storage import database
from sentrix.storage.models import Base, IncidentRecord


def feed(manager, ticks):
    """ticks: (level, tci, type, authorized, reason); 1 s apart."""
    incident = None
    for i, (level, tci, kind, authorized, reason) in enumerate(ticks):
        incident, _ = manager.update(level, tci, kind, now=float(i),
                                     features={"weapon": tci, "tick": float(i)},
                                     reason=reason, authorized=authorized,
                                     person_present=True,
                                     trigger={"tick": i})
    return incident


class TestPeakSnapshot:
    def test_every_field_comes_from_the_peak_tick(self):
        incident = feed(IncidentManager(), [
            (3, 0.45, "intrusion", False, "Unrecognised person"),
            (3, 0.56, "weapon", True, "weapon confirmed: scene 0.83"),
            (3, 0.50, "weapon", False, "weapon confirmed: scene 0.70"),
        ])
        peak = incident.peak
        assert (peak.tci, peak.incident_type, peak.authorized) == (0.56, "weapon", True)
        assert peak.reason == "weapon confirmed: scene 0.83"
        assert peak.features["tick"] == 1.0 and peak.trigger == {"tick": 1}
        assert incident.incident_type == "weapon", "type follows the peak, not the first tick"

    def test_first_tick_wins_a_tie(self):
        incident = feed(IncidentManager(), [(3, 0.5, "fire", False, "first"),
                                            (3, 0.5, "weapon", False, "second")])
        assert incident.peak_reason == "first"

    def test_quiet_ticks_inside_an_incident_are_counted(self):
        manager = IncidentManager()
        incident = feed(manager, [(3, 0.5, "fire", False, "x")] * 3)
        manager.update(1, 0.0, "normal", now=5.0)
        assert incident.frames == 4


class TestPersistOpenIncident:
    @staticmethod
    def pipeline():
        saved = []
        fake = SimpleNamespace(cfg=settings, session_id="s1",
                               _submit=lambda func, *args: saved.append(args))
        return fake, saved

    def test_saved_at_open_then_only_when_the_peak_moves(self):
        fake, saved = self.pipeline()
        manager = IncidentManager()
        persist = Pipeline._persist_open
        incident = feed(manager, [(3, 0.5, "fire", False, "a")])
        persist(fake, incident, 0.0)
        assert len(saved) == 1 and saved[0][2] is True, "saved as ongoing at open"
        persist(fake, incident, 1.0)
        assert len(saved) == 1, "unchanged peak is not rewritten"
        manager.update(3, 0.6, "fire", now=2.0, reason="b")
        persist(fake, incident, 2.0)
        assert len(saved) == 1, "peak moves are written at most every few seconds"
        persist(fake, incident, 2.0 + settings.incident.persist_interval_seconds)
        assert len(saved) == 2

    def test_escalation_is_written_at_once(self):
        """A hard kill after L4 -> L5 kept a row saying L4 (M3 acceptance run)."""
        fake, saved = self.pipeline()
        manager = IncidentManager()
        incident = feed(manager, [(4, 0.8, "weapon", False, "a")])
        Pipeline._persist_open(fake, incident, 0.0)
        manager.update(5, 0.9, "weapon", now=0.5, reason="b")
        Pipeline._persist_open(fake, incident, 0.5)
        assert len(saved) == 2 and incident.saved_level == 5

    def test_a_saved_incident_is_never_discarded_as_too_short(self):
        manager = IncidentManager()
        incident = feed(manager, [(3, 0.5, "fire", False, "a")])
        incident.saved_at = 0.0
        closed = manager._close(0.1)
        assert closed is incident


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(bind=engine, autocommit=False, autoflush=False))
    return engine


class TestDatabase:
    def test_open_row_then_closed_row(self, temp_db):
        database.init_db()
        incident = feed(IncidentManager(), [(3, 0.45, "intrusion", False, "a"),
                                            (4, 0.8, "weapon", True, "b")])
        database.save_incident(incident, "s1", ongoing=True)
        row = database.recent_incidents()[0]
        assert row["ended_at"] is None
        assert (row["incident_type"], row["reason"], row["authorized"]) == ("weapon", "b", True)
        assert row["trigger"] == {"tick": 1}
        database.save_incident(incident, "s1")
        assert database.recent_incidents()[0]["ended_at"] is not None

    def test_rows_left_open_by_a_crash_are_closed_on_start(self, temp_db):
        database.init_db()
        incident = feed(IncidentManager(), [(3, 0.5, "fire", False, "a")])
        database.save_incident(incident, "crashed", ongoing=True)
        assert database.close_interrupted("current") == 1
        assert database.recent_incidents()[0]["ended_at"] is not None

    def test_old_database_gains_the_new_column(self, temp_db):
        old = [c for c in IncidentRecord.__table__.columns if c.name != "trigger_json"]
        with temp_db.begin() as conn:
            cols = ", ".join(f"{c.name} {c.type.compile(temp_db.dialect)}" for c in old)
            conn.exec_driver_sql(f"CREATE TABLE incidents ({cols})")
        Base.metadata.create_all(bind=temp_db)            # leaves the old table alone
        database._migrate()
        with temp_db.begin() as conn:
            names = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(incidents)")}
        assert "trigger_json" in names


class TestEventLog:
    def test_every_row_reaches_the_disk_before_close(self, tmp_path):
        log = EventLog(directory=tmp_path, source="test")
        result = SimpleNamespace(
            tci=0.1, raw_tci=0.1, incident_type="normal", source="evidence",
            uncertainty=0.0,
            features=SimpleNamespace(**{name: 0.0 for name in
                                        ("vision", "audio", "motion", "behaviour",
                                         "identity", "weapon", "fire", "is_night")},
                                     authorized=False, person_present=False,
                                     person_count=0, behaviour_label="normal",
                                     audio_label="disabled", identity_degraded=False,
                                     extras={}))
        log.write(result, 1, "SECURE")
        rows = log.path.read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1 and json.loads(rows[0])["schema"] == 3


class TestAlarmBlock:
    def test_committed_alarm_is_kept_apart_from_the_tick(self):
        incident = feed(IncidentManager(), [(4, 0.8, "weapon", False, "weapon corroborated")])
        state = LevelState(level=4, status="HIGH", changed=False, since=1.0,
                           deescalate_in=3.2)
        block = Pipeline._alarm_block(None, 4, "HIGH", state, incident, now=6.0)
        assert block["incident_type"] == "weapon"
        assert block["headline"] == "weapon corroborated"
        assert block["since_s"] == 5.0 and block["deescalate_in"] == 3.2
        quiet = Pipeline._alarm_block(None, 1, "SECURE", state, None, now=6.0)
        assert quiet["incident_type"] == "normal" and quiet["incident_id"] is None


class TestVerdictsAndDispatch:
    def test_false_alarm_cancels_the_pending_dispatch(self, temp_db):
        from sentrix.alerting.dispatch import DispatchPackage

        database.init_db()
        incident = feed(IncidentManager(), [(4, 0.8, "weapon", False, "b")])
        database.save_incident(incident, "s1")
        package = DispatchPackage(
            id="p1", created_at="2026-10-01T00:00:00+00:00", incident_id=incident.id,
            incident_type="weapon", level=4, tci=0.8, user_name="u", user_address="a",
            user_phone="", camera_location="c", evidence_bundle=None,
            evidence_hash=None, authority="POLICE", status="PENDING", payload_json="{}")
        database.save_dispatch(package)
        assert database.set_verdict(incident.id, "FALSE")
        assert database.get_dispatch("p1")["status"] == "CANCELLED"
        assert database.recent_incidents()[0]["verdict"] == "FALSE"

    def test_dispatch_carries_the_sealed_evidence(self, monkeypatch):
        from sentrix.pipeline import engine as engine_mod

        made = {}
        monkeypatch.setattr(engine_mod.dispatch_service, "create",
                            lambda result, level, incident_id, evidence=None:
                            made.update(evidence=evidence, incident=incident_id))
        incident = feed(IncidentManager(), [(4, 0.8, "weapon", False, "b")])
        incident.evidence_bundle, incident.evidence_hash = "ev_1", "abc"
        Pipeline._create_dispatch(incident, SimpleNamespace(), 4)
        assert made["evidence"].bundle_id == "ev_1" and made["evidence"].sha256 == "abc"
        assert made["incident"] == incident.id


class TestMeasurementNeverWritesTheDatabase:
    def test_eval_pipeline_stubs_every_incident_write(self, monkeypatch):
        """The shutdown save once reached var/sentrix.db from a replay (M5 run)."""
        from sentrix.evaluation import harness
        calls = []
        monkeypatch.setattr(database, "save_incident", lambda *a, **k: calls.append(a))
        fake = Pipeline.__new__(Pipeline)
        fake.session_id = "s"
        for attr in ("_act", "_submit", "_save_open_incident"):
            setattr(fake, attr, lambda *a, **k: calls.append(attr))
        # build_eval_pipeline must override the same three hooks.
        source = Path(harness.__file__).read_text(encoding="utf-8")
        for attr in ("_act", "_submit", "_save_open_incident"):
            assert f"pipeline.{attr} = _noop" in source
        bench = (Path(__file__).parent.parent / "tools" / "bench_live.py").read_text(encoding="utf-8")
        assert "pipeline._save_open_incident = noop" in bench
