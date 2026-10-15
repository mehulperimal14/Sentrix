"""The talk-down speaker (decision D16): validation, presets, the rate limit,
single flight, the audit row, the LC route and the tunnel path table.

No test makes a sound: a recording ``FakePlayer`` stands in, and
``tests/conftest.py`` blocks the real players (and forces the speaker off on
the console backend for anything built implicitly)."""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest
from fastapi.testclient import TestClient

from sentrix.actor import Actor
from sentrix.config import settings
from sentrix.io import speaker as speaker_module
from sentrix.io.speaker import (PRESETS, ConsolePlayer, EspeakPlayer, Player, Speaker,
                                build_player, clean_text)
from sentrix.link.policy import TunnelPolicy, classify

PASSWORD = "speaker-test-password"
ON = dataclasses.replace(settings.speaker, enabled=True)


class FakePlayer(Player):
    name = "fake"
    real = True

    def __init__(self, hold: threading.Event | None = None) -> None:
        self.said: list[str] = []
        self.hold = hold

    def play(self, text: str) -> None:
        if self.hold is not None:
            self.hold.wait(2.0)
        self.said.append(text)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(cfg=ON, player=None, clock=None, background=False):
    audits: list[tuple[str, dict]] = []
    sp = Speaker(cfg, player=player or FakePlayer(), clock=clock or Clock(),
                 audit=lambda event, **detail: audits.append((event, detail)),
                 background=background)
    return sp, audits


OWNER = Actor("owner", id="u1", name="Riya", step_up_at=time.time() - 30)


class TestSpeaker:
    def test_off_by_default(self):
        assert settings.speaker.enabled is False             # conftest and the default
        sp, audits = make(cfg=settings.speaker)
        result = sp.say("hello")
        assert not result.ok and result.code == "conflict" and audits == []

    def test_a_preset_is_spoken_and_audited(self):
        sp, audits = make()
        result = sp.say(preset_id="leave", actor=OWNER)
        assert result.ok and result.text == PRESETS["leave"]
        assert sp.player.said == [PRESETS["leave"]]
        event, detail = audits[0]
        assert event == "speaker_say" and detail["by"] == OWNER.ref
        assert detail["preset"] == "leave" and detail["text"] == PRESETS["leave"]

    def test_free_text_is_cleaned_and_limited(self):
        sp, _ = make()
        assert sp.say("  Please\n\tleave \x07now  ").text == "Please leave now"
        assert sp.say("x" * 201).code == "invalid_args"
        assert sp.say("   ").code == "invalid_args"
        assert sp.say(preset_id="nope").code == "invalid_args"
        assert clean_text("a\x00b") == "a b"

    def test_three_a_minute_then_rate_limited(self):
        clock = Clock()
        sp, _ = make(clock=clock)
        for _ in range(3):
            assert sp.say("one").ok
        limited = sp.say("four")
        assert limited.code == "rate_limited" and 0 < limited.retry_after <= 60
        clock.t += 61
        assert sp.say("again").ok

    def test_one_message_at_a_time(self):
        hold = threading.Event()
        sp, _ = make(player=FakePlayer(hold), background=True)
        assert sp.say("first").ok
        busy = sp.say("second")
        assert busy.code == "conflict"
        hold.set()
        for _ in range(100):
            if not sp.status()["speaking"]:
                break
            time.sleep(0.01)
        assert sp.player.said == ["first"]

    def test_status_lists_presets_and_limits(self):
        sp, _ = make()
        status = sp.status()
        assert status["enabled"] and status["ready"] and status["max_chars"] == 200
        assert {p["id"] for p in status["presets"]} == set(PRESETS)

    def test_console_backend_logs_only(self):
        sp, _ = make(player=ConsolePlayer())
        result = sp.say("hello")
        assert result.ok and "logged only" in result.message
        assert sp.status()["ready"] is False

    def test_backend_choice(self):
        assert isinstance(build_player(dataclasses.replace(ON, backend="console")), ConsolePlayer)
        player = EspeakPlayer("espeak-ng", 150, 0.5, device="plughw:1,0")
        command = player.command("Leave now")
        assert command[:5] == ["espeak-ng", "-a", "100", "-s", "150"]
        assert "--stdout" in command and command[-1] == "Leave now"

    def test_real_players_are_blocked_in_tests(self):
        from conftest import DeviceBlocked
        with pytest.raises(DeviceBlocked):
            EspeakPlayer("espeak-ng", 150, 0.5).play("hello")


# ── routes ────────────────────────────────────────────────────────────────────
@pytest.fixture
def admin(monkeypatch):
    from sentrix.app import app
    from sentrix.web import auth
    from sentrix.web.security import generate_token
    monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
    client = TestClient(app)                  # no lifespan: no pipeline, no camera
    cookie = generate_token(PASSWORD)
    client.cookies.set("lc_session", cookie)
    client.headers["X-CSRF-Token"] = auth.csrf_for_session(cookie)
    return client


@pytest.fixture
def live_speaker():
    sp, audits = make()
    speaker_module._reset_speaker(sp)
    yield sp, audits
    speaker_module._reset_speaker()


class TestRoutes:
    def test_status_and_say(self, admin, live_speaker):
        sp, audits = live_speaker
        assert admin.get("/api/speaker").json()["backend"] == "fake"
        response = admin.post("/api/speaker/say", json={"preset_id": "help"})
        assert response.status_code == 200 and response.json()["text"] == PRESETS["help"]
        assert sp.player.said == [PRESETS["help"]]
        assert audits[0][1]["by"] == "lc:device_admin"

    def test_errors_carry_codes(self, admin, live_speaker):
        assert admin.post("/api/speaker/say", json={"text": "x" * 300}).status_code == 400
        assert admin.post("/api/speaker/say", json={"text": 5}).status_code == 400
        for _ in range(3):
            assert admin.post("/api/speaker/say", json={"text": "hi"}).status_code == 200
        limited = admin.post("/api/speaker/say", json={"text": "hi"})
        assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1

    def test_off_is_a_conflict(self, admin):
        speaker_module._reset_speaker(make(cfg=settings.speaker)[0])
        response = admin.post("/api/speaker/say", json={"preset_id": "help"})
        assert response.status_code == 409 and response.json()["code"] == "conflict"

    def test_needs_a_principal_and_csrf(self, admin, live_speaker, monkeypatch):
        from sentrix.app import app
        monkeypatch.setenv("SENTRIX_PASSWORD", PASSWORD)
        assert TestClient(app).post("/api/speaker/say", json={"text": "hi"}).status_code == 401
        del admin.headers["X-CSRF-Token"]
        assert admin.post("/api/speaker/say", json={"text": "hi"}).status_code == 403
        assert live_speaker[0].player.said == []

    def test_dashboard_shows_the_card_with_external_js(self, admin, live_speaker):
        page = admin.get("/dashboard").text
        assert 'id="speaker-card"' in page and "js/speaker.js" in page
        assert "onclick" not in page

    def test_node_page_reports_the_speaker(self, admin, live_speaker, monkeypatch):
        from types import SimpleNamespace
        from sentrix.web import node_routes
        pipeline = SimpleNamespace(siren=SimpleNamespace(name="console", is_real=False),
                                   source=SimpleNamespace(name="file", online=True))
        monkeypatch.setattr(node_routes, "_pipeline", lambda: pipeline)
        body = admin.get("/api/node").json()
        assert body["speaker"]["backend"] == "fake" and body["speaker"]["enabled"] is True


class TestTunnelRule:
    def test_owner_with_step_up_only(self):
        now = 1_800_000_000.0
        policy = TunnelPolicy(consents=lambda: {}, help_path_open=lambda: False,
                              clock=lambda: now)
        fresh = Actor("owner", id="u1", name="Riya", step_up_at=now - 30)
        assert classify("POST", "/api/speaker/say") == "speaker"
        assert policy.check_request("POST", "/api/speaker/say", fresh)
        stale = Actor("owner", id="u1", name="Riya", step_up_at=now - 3600)
        assert policy.check_request("POST", "/api/speaker/say", stale).code == "step_up_required"
        member = Actor("member", id="m1", name="Dev")
        assert policy.check_request("POST", "/api/speaker/say", member).code == "forbidden_role"
        operator = Actor("operator", id="d1", name="Desk")
        assert not policy.check_request("POST", "/api/speaker/say", operator)
        assert policy.check_request("GET", "/api/speaker", member)
