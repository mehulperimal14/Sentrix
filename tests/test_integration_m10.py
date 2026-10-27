"""Integration of the M10 tracks (docs/7 §15): the shared-file changes the
tracks asked for in their hand-off notes, and the seams between tracks that
no single track could test against the others' real code."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


# ── test isolation: the developer's var/ is never opened ─────────────────────
class TestVarIsolation:
    def test_the_device_database_lives_in_the_session_dir(self):
        from sentrix import paths
        from sentrix.storage import database
        real_var = (paths.ROOT_DIR / "var").resolve()
        assert paths.VAR_DIR.resolve() != real_var
        assert real_var not in paths.DB_PATH.resolve().parents
        assert str(paths.DB_PATH) in str(database.engine.url).replace("/", "\\") \
            or paths.DB_PATH.as_posix() in str(database.engine.url)
        for directory in (paths.EVIDENCE_DIR, paths.EVENTLOG_DIR, paths.SNAPSHOT_DIR,
                          paths.FACES_DIR, paths.CALIBRATION_DIR):
            assert real_var not in directory.resolve().parents


# ── default install: every service off, both apps start ───────────────────────
class TestDefaults:
    def test_no_service_is_built_on_a_default_install(self):
        from sentrix import services
        from sentrix.config import settings
        from sentrix.pipeline.state import SystemState
        registry = services.ServiceRegistry()
        ctx = services.ServiceContext(settings=settings, state=SystemState(),
                                      get_pipeline=lambda: None, registry=registry,
                                      extras={})
        assert registry.load_builtin(ctx) == [] and registry.names() == []

    def test_lc_app_builds_without_starting_anything(self):
        from fastapi.testclient import TestClient
        from sentrix.app import create_app
        client = TestClient(create_app())      # no lifespan: no pipeline, no camera
        assert client.get("/login").status_code == 200
        for path in ("/api/health", "/api/sos", "/api/node", "/api/link", "/api/channels"):
            assert client.get(path).status_code == 401, path

    def test_portal_app_serves_its_login_page(self, portal_app):
        from fastapi.testclient import TestClient
        with TestClient(portal_app) as client:
            page = client.get("/login")
        assert page.status_code == 200 and "SENTRIX" in page.text


# ── T-HW requests ──────────────────────────────────────────────────────────────
class TestHardwareConfig:
    def test_fields_exist_with_the_documented_defaults(self):
        from sentrix.config import settings
        gpio, node = settings.gpio, settings.node
        assert gpio.siren_max_on_seconds == 90.0 and gpio.siren_arm_pin is None
        assert gpio.siren_armed is True and gpio.tamper_pin is None
        assert node.bme280 is False and node.dht22 is False
        assert (node.picamera_main, node.picamera_lores) == ("1280x720", "640x480")

    def test_environment_overrides_the_field_at_call_time(self, monkeypatch):
        from sentrix.config import settings
        from sentrix.io import tunables
        import sentrix.config as config_module
        monkeypatch.delenv("SENTRIX_SIREN_MAX_ON_SECONDS", raising=False)
        patched = replace(settings, gpio=replace(settings.gpio, siren_max_on_seconds=30.0,
                                                 tamper_pin=22))
        monkeypatch.setattr(config_module, "settings", patched)
        assert tunables.siren_max_on_seconds() == 30.0 and tunables.tamper_pin() == 22
        monkeypatch.setenv("SENTRIX_SIREN_MAX_ON_SECONDS", "12")
        monkeypatch.setenv("SENTRIX_TAMPER_PIN", "99")          # invalid -> no pin
        assert tunables.siren_max_on_seconds() == 12.0 and tunables.tamper_pin() is None

    def test_live_loop_alive(self, monkeypatch):
        import sentrix.app as app_module
        from sentrix.io import node
        monkeypatch.setattr(app_module, "_thread", None)
        assert app_module.live_loop_alive() and node.live_loop_alive()
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        monkeypatch.setattr(app_module, "_thread", dead)
        app_module._stop.clear()
        assert not app_module.live_loop_alive() and not node.live_loop_alive()
        app_module._stop.set()
        try:
            assert app_module.live_loop_alive(), "stopping on purpose is not a hang"
        finally:
            app_module._stop.clear()

    def test_idle_node_block_has_the_running_shape(self):
        from sentrix.pipeline.state import NODE_IDLE, SystemState
        for key in ("temp_state", "throttled_raw", "throttle_flags", "under_voltage_occurred",
                    "power_w", "environment", "tamper", "platform", "telemetry"):
            assert key in NODE_IDLE
        assert SystemState().get()["node"]["telemetry"] is False


# ── T-CLIP requests ────────────────────────────────────────────────────────────
class TestClipIntegration:
    def test_clip_config_carries_the_engine_tunables(self):
        from sentrix.config import settings
        from sentrix.pipeline import clip
        for name, default in clip.DEFAULTS.items():
            assert getattr(settings.clip, name) == default, name
            assert clip.option(name) == default

    def test_idle_clip_block(self):
        from sentrix.pipeline.state import SystemState
        block = SystemState().get()["clip"]
        assert block["mode"] == "tick" and block["clips"] == 0

    def test_stale_overlay_limit_is_longer_in_clip_mode(self):
        from sentrix.config import settings
        from sentrix.pipeline.engine import Pipeline
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.cfg = settings
        pipeline._strategy = SimpleNamespace(name="tick")
        assert pipeline._stale_overlay_seconds() == settings.pipeline.stale_overlay_seconds
        pipeline._strategy = SimpleNamespace(name="clip")
        expected = settings.clip.hop_seconds * (1 + settings.clip.budget_fraction)
        assert pipeline._stale_overlay_seconds() == pytest.approx(
            max(settings.pipeline.stale_overlay_seconds, expected))

    def test_decide_passes_extra_to_the_event_log(self):
        import inspect
        from sentrix.pipeline import clip
        from sentrix.pipeline.engine import Pipeline
        assert "extra" in inspect.signature(Pipeline.decide).parameters
        assert "extra=extra" in inspect.getsource(clip.ClipRunner._decide)

    def test_replay_decisions_refuses_a_clip_session(self, tmp_path):
        from tools import replay_decisions
        tick = {"schema": 3, "features": {}, "ts": 1.0}
        log = tmp_path / "tick.jsonl"
        log.write_text(json.dumps(tick) + "\n", encoding="utf-8")
        assert replay_decisions.load(log) == [tick]
        for row in ({"schema": 4, "kind": "clip"},
                    {**tick, "extra": {"mode": "fast_lane"}}):
            clip_log = tmp_path / "clip.jsonl"
            clip_log.write_text(json.dumps(tick) + "\n" + json.dumps(row) + "\n",
                                encoding="utf-8")
            with pytest.raises(replay_decisions.ClipSessionError):
                replay_decisions.load(clip_log)

    def test_audio_window_and_sample_listeners(self, monkeypatch):
        from sentrix.config import settings
        from sentrix.perception.audio import AudioEngine, AudioResult
        engine = AudioEngine.__new__(AudioEngine)     # no model, no microphone
        engine.cfg = settings.windows
        engine._sample_listeners = []
        seen = []
        engine._classify = lambda samples, rms: (seen.append(len(samples))
                                                  or AudioResult(0.5, "scream", 0.9))
        quiet = engine.classify_window(np.zeros(16000, np.float32))
        assert quiet.label == "normal_ambient" and seen == []
        loud = engine.classify_window(np.full(16000, 0.5, np.float32))
        assert loud.label == "scream" and seen == [16000]
        got = []
        engine.add_sample_listener(lambda s, at: got.append((len(s), at)))
        engine.add_sample_listener(lambda s, at: 1 / 0)          # never stops capture
        engine._notify_samples(np.zeros(160, np.float32), 12.5)
        assert got == [(160, 12.5)]

    def test_clip_strategy_feeds_the_buffer_from_the_microphone(self):
        from sentrix.pipeline.clip import ClipStrategy
        listeners = []
        audio = SimpleNamespace(add_sample_listener=listeners.append,
                                remove_sample_listener=listeners.remove)
        added = []
        runner = SimpleNamespace(
            buffer=SimpleNamespace(add_audio=lambda s, at: added.append(at), latest_at=None),
            scheduler=SimpleNamespace(due=lambda latest: None), fast_lane=None)
        strategy = ClipStrategy(SimpleNamespace(audio=audio), runner=runner)
        stop = threading.Event()
        worker = threading.Thread(target=strategy.run, args=(stop,))
        worker.start()
        for _ in range(100):
            if listeners:
                break
            threading.Event().wait(0.01)
        listeners[0](np.zeros(10, np.float32), 3.0)
        stop.set()
        worker.join(timeout=5)
        assert added == [3.0] and listeners == []


# ── T-WEB requests ─────────────────────────────────────────────────────────────
class TestStateAndIncidents:
    def test_ws_payload_is_redacted(self):
        from sentrix.pipeline.state import SystemState
        state = SystemState()
        state.update(reason="camera rtsp://admin:hunter2@10.0.0.5/stream down")
        payload = state.ws_payload()
        assert "hunter2" not in payload and "admin:" not in payload

    def test_cloud_keys_follow_the_link(self):
        from sentrix.pipeline.state import LINK_IDLE, SystemState
        state = SystemState()
        assert state.get()["cloud_online"] is False
        state.update(link={**LINK_IDLE, "enabled": True, "connected": True})
        assert state.get()["cloud_online"] is True and state.get()["cloud_available"] is True

    def test_incident_filters_and_count(self, temp_db):
        from datetime import datetime, timedelta
        from sentrix.storage import database
        from sentrix.storage.models import IncidentRecord
        start = datetime(2026, 10, 1, 12, 0, 0)
        with database.session_scope() as session:
            for n, (kind, level, verdict) in enumerate([("weapon", 3, None), ("fire", 4, "TRUE"),
                                                        ("weapon", 5, "FALSE")]):
                session.add(IncidentRecord(id=f"i{n}", incident_type=kind, peak_level=level,
                                           peak_tci=0.5, verdict=verdict,
                                           started_at=start + timedelta(minutes=n)))
        assert database.count_incidents() == 3
        assert [r["id"] for r in database.recent_incidents(incident_type="weapon")] == ["i2", "i0"]
        assert database.count_incidents(min_level=4) == 2
        assert [r["id"] for r in database.recent_incidents(verdict="NONE")] == ["i0"]
        assert database.count_incidents(since=start + timedelta(minutes=1),
                                         until=start + timedelta(minutes=2)) == 1

    def test_link_outbox_never_reuses_a_seq(self, temp_db):
        from sqlalchemy import text
        with temp_db.connect() as conn:
            sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name='link_outbox'")
                               ).scalar()
        assert "AUTOINCREMENT" in sql.upper()


# ── T-CH requests ──────────────────────────────────────────────────────────────
class TestChannels:
    def test_router_waits_at_least_the_providers_retry_after(self):
        from sentrix.alerting.channels.base import (Channel, MemoryOutbox, Message,
                                                    Recipient, SendResult)
        from sentrix.alerting.channels.router import Router
        from conftest import live_test_policy

        class Throttled:
            name = "telegram"

            def send(self, recipient, message):
                return SendResult(False, error="429", retry_after=500.0)

        now = [1000.0]
        router = Router({"telegram": Channel("telegram", Throttled(),
                                             policy_factory=live_test_policy)},
                        MemoryOutbox(), clock=lambda: now[0])
        item_id = router.enqueue(Recipient("owner", "R", "telegram", "tg:12345"),
                                 Message(template="sos.prompt", step="t0", text="hi",
                                         request_id="r1"))
        router.pump()
        (item,) = [i for i in router.outbox.items() if i.id == item_id]
        assert item.status == "FAILED" and item.next_attempt_at == pytest.approx(1500.0)
        assert not math.isinf(item.next_attempt_at)

    def test_portal_settings_carry_telegram_mode_and_grievance(self, monkeypatch, tmp_path):
        from sentrix_portal.settings import PortalSettings
        monkeypatch.setenv("SENTRIX_TELEGRAM_MODE", "Webhook")
        monkeypatch.setenv("PORTAL_GRIEVANCE_CONTACT", "grievance@example.in")
        settings = PortalSettings.from_env(data_dir=str(tmp_path))
        assert settings.telegram_mode == "webhook"
        assert settings.grievance_contact == "grievance@example.in"

    def test_sos_delivery_status_reaches_the_sos_service(self, portal_app, monkeypatch):
        from sentrix.alerting.channels.base import Message, OutboxItem, Recipient
        from sentrix_portal.services import notify, sos
        calls = []
        monkeypatch.setattr(sos, "notification_status",
                            lambda db, **kw: calls.append(kw) or "cloud")
        worker = notify.NotifyWorker(portal_app)
        owner = Recipient("owner", "R", "telegram", "tg:1")
        worker.sos_feedback(OutboxItem("n1", owner, Message(template="sos.prompt", step="t0",
                                                            text="x", request_id="r1"),
                                       status="SENT"))
        worker.sos_feedback(OutboxItem("n2", owner, Message(template="account.reset",
                                                            step="s", text="x"),
                                       status="SENT"))
        assert calls == [{"request_id": "r1", "channel": "telegram", "recipient_ref": "owner",
                          "step": "t0", "status": "SENT", "error": None}]


# ── T-ACCT / T-LINK / T-SOS seams ─────────────────────────────────────────────
class TestPortalSeams:
    def test_the_preview_uses_the_device_wording(self):
        from sentrix.sos import templates
        from sentrix_portal.services import accounts
        profile = {"test_mode": True, "owner": {"name": "Riya"}}
        rendered = accounts.render_sos_preview(profile, incident_type="fire")
        expected = templates.preview(profile, incident_type="fire", test_mode=True)
        assert rendered == {"full": expected["full"], "sms": expected["sms"]}
        assert "SENTRIX does not contact 112 itself" in rendered["full"]

    def test_base_page_has_the_banner_and_the_account_link(self, portal_app):
        template = portal_app.state.templates.get_template("base.html")
        owner = SimpleNamespace(role="owner", csrf_token="t", display_name="Riya")
        html = template.render(principal=owner, csp_nonce="n0nce", test_mode=True)
        assert html.count("/static/sos/sos.js") == 1 and 'nonce="n0nce"' in html
        assert 'href="/account"' in html and "/static/sos/sos.css" in html
        operator = SimpleNamespace(role="operator", csrf_token="t", display_name="Desk")
        assert "/static/sos/sos.js" not in template.render(principal=operator, csp_nonce="n")


# ── tools ─────────────────────────────────────────────────────────────────────
class TestDoctor:
    def test_a_video_file_camera_is_never_probed(self, monkeypatch, tmp_path):
        import cv2
        from tools import doctor
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x")
        monkeypatch.setattr(cv2, "VideoCapture",
                            lambda *a, **k: pytest.fail("the doctor opened a camera"))
        monkeypatch.setenv("CAMERA_SOURCES", f"{video},rtsp://u:pw@cam/1")
        report = doctor.Report()
        doctor.check_camera(report)
        details = " ".join(row[2] for row in report.rows)
        assert "video file" in details and "pw" not in details

    def test_exports_section(self, monkeypatch, tmp_path):
        import sentrix.runtime as runtime
        from tools import doctor
        monkeypatch.setattr(runtime, "resolve_backend", lambda backend=None: "torch")
        report = doctor.Report()
        doctor.check_exports(report, directory=tmp_path)
        assert "torch" in report.rows[-1][2]
        monkeypatch.setattr(runtime, "resolve_backend", lambda backend=None: "onnx")
        report = doctor.Report()
        doctor.check_exports(report, directory=tmp_path)
        assert "falls back to torch" in report.rows[-1][2]
        (tmp_path / "yolo_640.onnx").write_bytes(b"x")
        (tmp_path / "manifest.json").write_text(json.dumps(
            {"exports": {"yolo_640.onnx": {"parity": {"parity_ok": True}}}}), encoding="utf-8")
        report = doctor.Report()
        doctor.check_exports(report, directory=tmp_path)
        assert any(row[1] == "export yolo_640.onnx" and row[2] == "parity OK"
                   for row in report.rows)

    def test_no_camera_flag(self):
        import inspect
        from tools import doctor
        assert "--no-camera" in inspect.getsource(doctor.main)


class TestCommandLines:
    @pytest.mark.parametrize("module", ["sentrix.app", "sentrix_portal.__main__"])
    def test_help_starts_nothing(self, module, monkeypatch, capsys):
        import importlib
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("server started"))
        main = importlib.import_module(module).main
        with pytest.raises(SystemExit) as exit_info:
            main(["--help"])
        assert exit_info.value.code == 0 and "--port" in capsys.readouterr().out
