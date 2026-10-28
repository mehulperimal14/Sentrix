"""Regression tests for the adversarial review (safety lens): test mode
fails closed, and tests can open neither a webcam nor a microphone."""

from __future__ import annotations

import pytest


class TestTestModeFailsClosed:
    @pytest.mark.parametrize("raw", ["y", "enabled", "ture", "On!", "2"])
    def test_an_unrecognised_value_keeps_test_mode_on(self, monkeypatch, raw):
        from sentrix.alerting import guard
        from sentrix_portal.settings import PortalSettings
        monkeypatch.setenv("SENTRIX_TEST_MODE", raw)
        monkeypatch.setenv("PORTAL_TEST_MODE", raw)
        assert guard.test_mode_flag("SENTRIX_TEST_MODE") is True
        assert guard.SendPolicy.from_env().test_mode is True
        assert guard.SendPolicy.from_env(test_mode_env="PORTAL_TEST_MODE").test_mode is True
        assert PortalSettings.from_env().test_mode is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", "OFF"])
    def test_only_an_explicit_falsy_value_turns_it_off(self, monkeypatch, raw):
        from sentrix.alerting import guard
        monkeypatch.setenv("SENTRIX_TEST_MODE", raw)
        assert guard.SendPolicy.from_env().test_mode is False

    def test_unset_is_on(self, monkeypatch):
        from sentrix.alerting import guard
        monkeypatch.delenv("SENTRIX_TEST_MODE", raising=False)
        assert guard.test_mode_flag() is True


class TestDeviceGuard:
    def test_environment_has_no_camera_and_no_mic(self):
        import os
        assert not os.environ["CAMERA_SOURCES"].strip().isdigit()
        assert os.environ["SENTRIX_AUDIO"] == "0"

    def test_webcam_index_is_blocked(self):
        import cv2
        with pytest.raises(RuntimeError, match="device guard"):
            cv2.VideoCapture(0)

    def test_capture_source_on_an_index_is_blocked(self):
        from sentrix.io import sources
        with pytest.raises(RuntimeError, match="device guard"):
            sources.CaptureSource(0)
        with pytest.raises(RuntimeError, match="device guard"):
            sources.build_source("1")

    def test_microphone_is_blocked(self):
        sd = pytest.importorskip("sounddevice")
        with pytest.raises(RuntimeError, match="device guard"):
            sd.rec(10, samplerate=16000, channels=1)


class TestNoEmergencyNumberInTestMode:
    @pytest.mark.parametrize("desk", ["112", "+91112", "100", "108"])
    def test_portal_call_button_never_dials_an_emergency_number(self, desk):
        from types import SimpleNamespace
        from sentrix_portal.services.sos_views import call_112
        target = call_112(SimpleNamespace(test_mode=True, desk_number=desk))
        assert target["href"] is None and "112" not in (target["href"] or "")

    def test_portal_call_button_uses_a_real_desk_number(self):
        from types import SimpleNamespace
        from sentrix_portal.services.sos_views import call_112
        target = call_112(SimpleNamespace(test_mode=True, desk_number="+919800000001"))
        assert target["href"] == "tel:+919800000001"

    def test_sos_texts_never_say_call_112_in_test_mode(self):
        from sentrix.sos import templates
        assert templates.call_number({"desk": {"phone": "112"}}, True) == "the SENTRIX desk"
        assert templates.call_number({"desk": {"phone": "+919800000001"}}, True) == \
            "+919800000001"
        assert templates.call_number({}, False) == "112"


class TestHardwareParity:
    def test_the_siren_stops_after_the_requested_duration(self, monkeypatch):
        from sentrix.io import actuators
        played = {}

        class Proc:
            def __init__(self, args):
                played["args"] = args

            def terminate(self):
                played["stopped_after"] = played.get("slept")

        monkeypatch.setattr(actuators.platform, "system", lambda: "Linux")
        monkeypatch.setattr(actuators.subprocess, "Popen", Proc)
        monkeypatch.setattr(actuators.time, "sleep",
                            lambda s: played.__setitem__("slept", s))
        siren = actuators.SoundSiren()
        if not siren.sound_path.exists():
            pytest.skip("siren.wav not bundled")
        siren._play(1.0)
        assert played["stopped_after"] == 1.0

    def test_a_pir_edge_reaches_the_clip_buffer(self, monkeypatch):
        import sys
        from types import SimpleNamespace
        from sentrix.io import gpio
        from sentrix.pipeline.clip import ClipBuffer
        buffer = ClipBuffer(max_fps=15.0)
        fake_engine = SimpleNamespace(_pipeline=SimpleNamespace(
            _strategy=SimpleNamespace(runner=SimpleNamespace(buffer=buffer))))
        monkeypatch.setitem(sys.modules, "sentrix.pipeline.engine", fake_engine)
        assert gpio.forward_sensor_event("pir")
        assert gpio.forward_sensor_event("door", door="open")
        assert [e.kind for e in buffer._events] == ["pir", "door"]

    def test_no_pipeline_no_event_and_none_is_built(self, monkeypatch):
        import sys
        from types import SimpleNamespace
        from sentrix.io import gpio
        monkeypatch.setitem(sys.modules, "sentrix.pipeline.engine",
                            SimpleNamespace(_pipeline=None))
        assert gpio.forward_sensor_event("pir") is False

    def test_the_capture_preview_never_drains_the_tunnel(self):
        """The old Access page polled the preview through the Portal. The
        device app's Faces page, which polls it, runs on the LAN only (its
        page is not in the path table), and through the tunnel the preview
        stays inside the per-user media budget."""
        from sentrix.actor import Actor
        from sentrix.link.policy import TunnelPolicy, classify
        assert classify("GET", "/admin/faces") == "unknown"
        assert classify("GET", "/api/authorized/preview.jpg") == "media"
        policy = TunnelPolicy(media_per_minute=3, consents=lambda: {"remote_view": True},
                              help_path_open=lambda: False)
        owner = Actor("owner", id="u1", name="Riya")
        for _ in range(3):
            assert policy.check_request("GET", "/api/authorized/preview.jpg", owner)
        limited = policy.check_request("GET", "/api/authorized/preview.jpg", owner)
        assert limited.code == "rate_limited" and limited.retry_after
