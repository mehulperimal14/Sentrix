"""Regression tests for incidents, escalation, evidence and web security."""

from __future__ import annotations

import json

import pytest

from sentrix.config import settings
from sentrix.pipeline.escalation import plan_for
from sentrix.pipeline.incidents import IncidentManager


class TestEscalationLadder:
    """The shipped ladder was two steps more aggressive than every document:
    SMS at level 2 and siren at level 3. Combined with the fusion bug pinning
    the system at level 2, that produced the alert storm."""

    def test_level_1_does_nothing(self):
        plan = plan_for(1)
        assert not plan.any_side_effect and plan.log

    def test_level_2_does_not_message_anyone(self):
        plan = plan_for(2)
        assert not plan.sms, "SMS at level 2 caused the alert storm"
        assert not plan.siren and not plan.call and not plan.evidence

    def test_level_3_does_not_sound_the_siren(self):
        plan = plan_for(3)
        assert plan.evidence
        assert not plan.siren, "siren at level 3 is two steps early"
        assert not plan.call

    def test_level_4_messages_and_prepares_dispatch(self):
        plan = plan_for(4)
        assert plan.sms and plan.dispatch and plan.evidence
        assert not plan.call

    def test_level_5_is_full_escalation(self):
        plan = plan_for(5)
        assert all((plan.sms, plan.siren, plan.call, plan.dispatch,
                    plan.evidence, plan.snapshot))

    def test_out_of_range_is_clamped(self):
        assert plan_for(99).level == 5 and plan_for(-3).level == 1

    def test_ladder_is_monotonic(self):
        """No action may switch off as severity increases."""
        fields = ("snapshot", "evidence", "sms", "siren", "dispatch", "call")
        for lower, upper in zip(range(1, 5), range(2, 6)):
            for field in fields:
                if getattr(plan_for(lower), field):
                    assert getattr(plan_for(upper), field), (
                        f"{field} is on at L{lower} but off at L{upper}")


class TestIncidentGrouping:
    """Side effects bind to incidents, not frames. The previous per-frame model
    produced 1,489 evidence bundles and 2,415 snapshots from one false positive."""

    def test_quiet_scene_opens_nothing(self):
        manager = IncidentManager()
        active, closed = manager.update(1, 0.05, "normal", now=0.0)
        assert active is None and closed is None

    def test_opening_tick_reason_is_kept_when_it_is_the_peak(self):
        """An incident that only declines after opening kept an empty reason."""
        manager = IncidentManager()
        manager.update(3, 0.45, "intrusion", now=0.0, features={"weapon": 0.0},
                       reason="Unrecognised person present for 3 s (armed mode)")
        active, _ = manager.update(3, 0.40, "intrusion", now=0.5, reason="later")
        assert active.peak_reason.startswith("Unrecognised person")
        assert active.peak_features == {"weapon": 0.0}

    def test_sustained_threat_is_one_incident(self):
        manager = IncidentManager()
        ids = set()
        now = 0.0
        for _ in range(600):                       # 100 s at 6 Hz
            now += 1 / 6
            active, _ = manager.update(4, 0.75, "intrusion", now=now)
            if active:
                ids.add(active.id)
        assert len(ids) == 1, f"one continuous threat became {len(ids)} incidents"

    def test_each_action_fires_once_per_incident(self):
        manager = IncidentManager()
        incident, _ = manager.update(5, 0.95, "weapon", now=1.0)
        assert manager.claim(incident, "sms") is True
        assert manager.claim(incident, "sms") is False
        assert manager.claim(incident, "call") is True

    def test_evidence_is_rate_limited(self):
        manager = IncidentManager()
        incident, _ = manager.update(4, 0.8, "intrusion", now=1.0)
        assert manager.should_capture_evidence(incident, now=1.0)
        incident.evidence_count, incident.last_evidence_at = 1, 1.0
        assert not manager.should_capture_evidence(incident, now=2.0)
        later = 1.0 + settings.incident.evidence_interval_seconds + 1
        assert manager.should_capture_evidence(incident, now=later)

    def test_incident_closes_after_quiet_period(self):
        manager = IncidentManager()
        manager.update(4, 0.8, "intrusion", now=1.0)
        quiet = settings.incident.close_after_quiet_seconds
        assert manager.update(1, 0.05, "normal", now=2.0)[1] is None
        closed = manager.update(1, 0.05, "normal", now=2.0 + quiet + 1)[1]
        assert closed is not None and closed.peak_level == 4

    def test_peak_level_is_retained(self):
        manager = IncidentManager()
        manager.update(2, 0.30, "intrusion", now=1.0)
        manager.update(5, 0.95, "weapon", now=1.2)
        active, _ = manager.update(2, 0.30, "intrusion", now=1.4)
        assert active.peak_level == 5


class TestEvidenceVault:
    def test_filenames_do_not_collide(self, tmp_path):
        """Names had one-second resolution, so same-second writes overwrote."""
        from sentrix.evidence.vault import EvidenceVault
        vault = EvidenceVault(directory=tmp_path)
        names = {vault._bundle_name(5) for _ in range(500)}
        assert len(names) == 500, "evidence filenames still collide"

    def test_roundtrip_encrypt_and_verify(self, tmp_path):
        pytest.importorskip("cv2")
        import numpy as np
        from sentrix.evidence.vault import EvidenceVault
        from sentrix.fusion.features import FeatureVector
        from sentrix.fusion.tci import TCIResult

        vault = EvidenceVault(directory=tmp_path)
        frame = np.full((64, 64, 3), 127, dtype=np.uint8)
        result = TCIResult(tci=0.9, raw_tci=0.9, incident_type="weapon",
                           reason="test", source="override",
                           features=FeatureVector(weapon=0.9))

        record = vault.save(frame, result, 5, "CRITICAL", incident_id="i1")
        assert record is not None
        ok, note = vault.verify_bundle(record.bundle_id)
        assert ok, note
        assert vault.decrypt(record.bundle_id) is not None
        assert vault.verify_chain()["ok"]

    def test_sidecar_records_why_the_frame_was_sealed(self, tmp_path):
        """Live sidecars held only repr strings of YOLO boxes (CLAUDE.md M3)."""
        pytest.importorskip("cv2")
        import numpy as np
        from sentrix.evidence.vault import EvidenceVault
        from sentrix.fusion.features import FeatureVector
        from sentrix.fusion.tci import TCIResult

        vault = EvidenceVault(directory=tmp_path)
        result = TCIResult(tci=0.5, raw_tci=0.5, incident_type="weapon",
                           reason="weapon confirmed: scene 0.83/0.50 (3/3 ticks)",
                           source="evidence", features=FeatureVector(weapon=0.5))
        box = {"label": "person", "conf": 0.9, "bbox": [1, 2, 3, 4], "track_id": 7,
               "global_id": 1}
        context = {"session": "pipe123", "trigger": {"weapon": {"state": "confirmed"}},
                   "scene": {"probs": {"weapon": 0.83}}}
        record = vault.save(np.zeros((32, 32, 3), np.uint8), result, 3, "ELEVATED",
                            incident_id="i1", detections=[box], context=context)
        sidecar = json.loads((tmp_path / f"{record.bundle_id}.json").read_text())
        assert sidecar["detections"] == [{"label": "person", "conf": 0.9,
                                          "bbox": [1, 2, 3, 4], "track_id": 7}]
        assert sidecar["extras"]["session"] == "pipe123"
        assert sidecar["extras"]["trigger"]["weapon"]["state"] == "confirmed"
        assert sidecar["extras"]["scene"]["probs"]["weapon"] == 0.83
        assert vault.verify_chain()["ok"]

    def test_tampering_breaks_the_chain(self, tmp_path):
        pytest.importorskip("cv2")
        import numpy as np
        from sentrix.evidence.vault import EvidenceVault
        from sentrix.fusion.features import FeatureVector
        from sentrix.fusion.tci import TCIResult

        vault = EvidenceVault(directory=tmp_path)
        frame = np.full((64, 64, 3), 200, dtype=np.uint8)
        result = TCIResult(tci=0.9, raw_tci=0.9, incident_type="fire",
                           reason="test", source="override",
                           features=FeatureVector(fire=0.9))
        records = [vault.save(frame, result, 5, "CRITICAL") for _ in range(3)]
        assert vault.verify_chain()["ok"]

        # Rewrite one bundle *and* its sidecar hash, exactly as an attacker
        # with disk access would. The unsigned per-file hash alone would not
        # have caught this; the HMAC chain does.
        target = records[1]
        enc = tmp_path / f"{target.bundle_id}.enc"
        enc.write_bytes(b"forged payload")
        meta_path = tmp_path / f"{target.bundle_id}.json"
        meta = json.loads(meta_path.read_text())
        import hashlib
        meta["sha256"] = hashlib.sha256(b"forged payload").hexdigest()
        meta_path.write_text(json.dumps(meta))

        report = vault.verify_chain()
        assert not report["ok"], "tampered evidence passed verification"


class TestWebSecurity:
    def test_token_roundtrip(self):
        from sentrix.web.security import generate_token, verify_token
        token = generate_token("hunter2")
        assert verify_token(token, "hunter2")
        assert not verify_token(token, "wrong")
        assert not verify_token(None, "hunter2")
        assert not verify_token("garbage", "hunter2")

    @pytest.mark.parametrize("raw,expected_blocked", [
        ("../../etc/passwd", True),
        ("..\\..\\windows\\system32", True),
        ("normal.jpg", False),
    ])
    def test_filename_sanitisation(self, raw, expected_blocked):
        from sentrix.web.security import sanitize_filename
        cleaned = sanitize_filename(raw)
        assert ".." not in cleaned and "/" not in cleaned and "\\" not in cleaned
        if not expected_blocked:
            assert cleaned == raw

    def test_upload_requires_real_image_bytes(self):
        from sentrix.web.security import validate_image
        jpeg = b"\xff\xd8\xff" + b"\x00" * 32
        assert validate_image("a.jpg", "image/jpeg", jpeg)[0]
        # Correct extension and MIME, but the bytes are a script.
        assert not validate_image("a.jpg", "image/jpeg", b"<?php echo 1; ?>")[0]
        assert not validate_image("a.exe", "application/x-msdownload", jpeg)[0]

    def test_login_is_rate_limited(self):
        from sentrix.web.security import RateLimiter
        limiter = RateLimiter(max_attempts=3, window_seconds=60)
        assert all(limiter.allow("1.2.3.4") for _ in range(3))
        assert not limiter.allow("1.2.3.4")
        assert limiter.allow("5.6.7.8")


class TestRuntimeIsolation:
    """Nothing sensitive may sit under a static mount."""

    def test_var_is_outside_the_static_directory(self):
        from sentrix.paths import (EVIDENCE_DIR, FACES_DIR, SNAPSHOT_DIR,
                                   STATIC_DIR)
        static = STATIC_DIR.resolve()
        for directory in (EVIDENCE_DIR, SNAPSHOT_DIR, FACES_DIR):
            with pytest.raises(ValueError):
                directory.resolve().relative_to(static)
