"""T-LINK: the device-enforced rules (docs/7 §5.6, §5.7).

The tunnel path table (``sentrix/link/policy.py``) and the link commands
(``sentrix/link/commands.py``): roles, consents from the pushed profile,
step-up, rate limits, the Desk rule, the SMS allowlist and daily cap, the
shared siren-test limiter, test-mode-only demo commands, and an audit row for
everything. No command reaches a real channel: the channels service is a
recording fake.
"""

from __future__ import annotations

import time

import pytest

from sentrix.actor import Actor
from sentrix.link.policy import TunnelPolicy, classify, normalise_path

from test_link_support import (CONTACT_PHONE, DESK_PHONE, OWNER_PHONE, FakeChannels,
                               FakePipeline, FakeSosService)

NOW = 1_760_430_000.0
OWNER = Actor("owner", id="u1", name="Riya", step_up_at=NOW - 30)
STALE_OWNER = Actor("owner", id="u1", name="Riya", step_up_at=NOW - 3600)
MEMBER = Actor("member", id="m1", name="Dev")
DESK = Actor("operator", id="op1", name="Kartik")
CONTACT = Actor("contact", id="c1", name="Arjun")
ALL = {"remote_view": True, "face_recognition": True, "desk_live_view": True}


def policy(consents=None, help_path=False, media=60):
    return TunnelPolicy(media_per_minute=media, consents=lambda: dict(consents or {}),
                        help_path_open=lambda: help_path, clock=lambda: NOW)


class TestPathTable:
    def test_paths_are_normalised_or_refused(self):
        assert normalise_path("/dashboard") == "/dashboard"
        assert normalise_path("/media/face/%61.jpg") == "/media/face/a.jpg"
        for bad in ("dashboard", "/../etc/passwd", "/media/face/%2e%2e/x", "//evil.example/x",
                    "/a\\b", "/x\x00", "http://h/x", "/a/./b"):
            assert normalise_path(bad) is None, bad

    def test_classes(self):
        assert classify("GET", "/dashboard") == "page"
        assert classify("GET", "/api/health") == "read_api"
        assert classify("GET", "/static/js/app.js") == "static"
        assert classify("GET", "/media/snapshot/x.jpg") == "media"
        assert classify("GET", "/api/authorized/preview.jpg") == "media"
        assert classify("GET", "/media/face/riya.jpg") == "faces"
        assert classify("POST", "/api/authorized/capture") == "gallery_write"
        assert classify("DELETE", "/api/authorized/riya.jpg") == "gallery_delete"
        assert classify("POST", "/api/incidents/i1/verdict") == "sos_write"
        assert classify("POST", "/api/sos/requests/r/approve") == "sos_write"
        assert classify("POST", "/api/dispatch/p/send/POLICE") == "sos_write"
        assert classify("POST", "/api/mode") == "mode"
        assert classify("POST", "/api/node/siren-test") == "siren_test"
        assert classify("POST", "/api/link/reconnect") == "lc_only"
        assert classify("GET", "/api/recordings") == "lc_only"
        assert classify("POST", "/api/recordings/start") == "lc_only"
        assert classify("DELETE", "/api/recordings/rec_20261028_101010") == "lc_only"
        assert classify("GET", "/media/recording/rec_20261028_101010.avi") == "lc_only"
        assert classify("HEAD", "/media/recording/rec_20261028_101010.wav") == "lc_only"
        assert classify("GET", "/ws/threat") == "never"
        assert classify("POST", "/api/whatever") == "unknown"

    @pytest.mark.parametrize("path", ["/login", "/logout", "/setup/wifi", "/docs", "/redoc",
                                      "/openapi.json", "/video", "/ws/threat"])
    def test_never_tunnelled(self, path):
        decision = policy(ALL).check_request("GET", path, OWNER)
        assert not decision and decision.code == "not_found" and decision.status == 404

    def test_owner_and_member_read_pages_and_apis(self):
        p = policy()
        for path in ("/dashboard", "/live", "/events", "/alerts", "/evidence", "/dispatch",
                     "/authorized", "/sos", "/node", "/", "/api/health", "/static/css/style.css"):
            assert p.check_request("GET", path, OWNER), path
            assert p.check_request("GET", path, MEMBER), path
        assert not p.check_request("GET", "/no-such-page", OWNER)

    def test_media_needs_remote_view_and_faces_face_recognition(self):
        assert policy({}).check_request("GET", "/media/snapshot/a.jpg", OWNER).code == \
            "consent_required"
        assert policy({"remote_view": True}).check_request("GET", "/media/snapshot/a.jpg",
                                                           MEMBER)
        faces = policy({"remote_view": True}).check_request("GET", "/media/face/a.jpg", OWNER)
        assert faces.code == "consent_required" and "face" in faces.reason
        assert policy(ALL).check_request("GET", "/media/face/a.jpg", OWNER)

    def test_media_rate_limit_is_per_user(self):
        p = policy(ALL, media=3)
        assert all(p.check_request("GET", "/media/snapshot/a.jpg", OWNER) for _ in range(3))
        limited = p.check_request("GET", "/media/snapshot/a.jpg", OWNER)
        assert limited.code == "rate_limited" and limited.status == 429 and limited.retry_after
        assert p.check_request("GET", "/media/snapshot/a.jpg", MEMBER), "another user's bucket"

    def test_gallery_writes_owner_consent_and_step_up_for_delete(self):
        p = policy(ALL)
        assert p.check_request("POST", "/api/authorized/capture", OWNER)
        assert p.check_request("POST", "/authorized/upload", OWNER)
        assert p.check_request("POST", "/api/authorized/capture", MEMBER).code == "forbidden_role"
        assert policy({"remote_view": True}).check_request(
            "POST", "/api/authorized/capture", OWNER).code == "consent_required"
        assert p.check_request("DELETE", "/api/authorized/x.jpg", OWNER)
        assert p.check_request("DELETE", "/api/authorized/x.jpg",
                               STALE_OWNER).code == "step_up_required"

    def test_sos_verdict_dispatch_mode_for_owner_and_member(self):
        p = policy()
        for path in ("/api/incidents/i/verdict", "/api/sos/requests/r/decline",
                     "/api/dispatch/p/send/FIRE", "/api/mode"):
            assert p.check_request("POST", path, OWNER) and p.check_request("POST", path, MEMBER)

    def test_siren_test_owner_with_step_up_only(self):
        p = policy()
        assert p.check_request("POST", "/api/node/siren-test", OWNER)
        assert p.check_request("POST", "/api/node/siren-test",
                               STALE_OWNER).code == "step_up_required"
        assert p.check_request("POST", "/api/node/siren-test", MEMBER).code == "forbidden_role"

    def test_lc_only_routes_and_unknown_writes_refused(self):
        p = policy(ALL)
        assert p.check_request("POST", "/api/link/reconnect", OWNER).code == "forbidden_role"
        assert p.check_request("POST", "/api/channels/test", OWNER).code == "forbidden_role"
        assert p.check_request("POST", "/api/config", OWNER).code == "forbidden_role"
        assert p.check_request("TRACE", "/dashboard", OWNER).code == "invalid_args"

    @pytest.mark.parametrize("method, path", [
        ("GET", "/api/recordings"), ("POST", "/api/recordings/start"),
        ("POST", "/api/recordings/stop"), ("DELETE", "/api/recordings/rec_20261028_101010"),
        ("GET", "/media/recording/rec_20261028_101010.avi"),
        ("HEAD", "/media/recording/rec_20261028_101010.json"),
        ("GET", "/media/recording/rec%5F20261028%5F101010.avi")])
    def test_training_recordings_stay_on_the_lan(self, method, path):
        """D20: neither the read API nor the media class lets a recording out,
        whatever the role, consent or step-up (D12, the 16 MiB response cap)."""
        p = policy(ALL, help_path=True)
        for actor in (OWNER, MEMBER, DESK):
            decision = p.check_request(method, path, actor)
            assert not decision and decision.code == "forbidden_role", (actor.type, path)
            assert decision.path_class == "lc_only" and decision.status == 403

    def test_desk_only_on_the_help_path_with_consent(self):
        assert policy(ALL, help_path=False).check_request("GET", "/live", DESK).code == \
            "forbidden_role"
        assert policy({}, help_path=True).check_request("GET", "/live", DESK).code == \
            "consent_required"
        allowed = policy(ALL, help_path=True)
        for path in ("/dashboard", "/live", "/api/health", "/api/metrics", "/static/js/app.js"):
            assert allowed.check_request("GET", path, DESK), path
        for path in ("/events", "/evidence", "/media/snapshot/a.jpg", "/authorized"):
            assert not allowed.check_request("GET", path, DESK), path
        assert not allowed.check_request("POST", "/api/incidents/i/verdict", DESK)
        assert allowed.check_video(DESK) and not policy(ALL).check_video(DESK)

    def test_video_and_contacts(self):
        assert policy({"remote_view": True}).check_video(MEMBER)
        assert policy({}).check_video(OWNER).code == "consent_required"
        assert policy(ALL).check_video(CONTACT).code == "forbidden_role"
        assert policy(ALL).check_request("GET", "/dashboard", CONTACT).code == "forbidden_role"

    def test_desk_rule_reads_the_device_request_store(self, temp_db):
        from sentrix.storage import requests
        real = TunnelPolicy(media_per_minute=60, consents=lambda: ALL)
        assert not real.check_request("GET", "/live", DESK)
        requests.create_request("r-help", incident_id="i", incident_type="weapon", level=3,
                                evidence_state="confirmed", policy={}, expires_at=None,
                                test_mode=True)
        requests.transition("r-help", "prompted")
        assert not real.check_request("GET", "/live", DESK), "PENDING is not the help path"
        requests.transition("r-help", "approve")
        assert real.check_request("GET", "/live", DESK)

    def test_consents_come_from_the_pushed_profile(self, temp_db):
        from sentrix.link import profile, protocol
        from test_link_support import Seeded, profile_for
        real = TunnelPolicy(media_per_minute=60)
        assert real.check_request("GET", "/media/snapshot/a.jpg", OWNER).code == \
            "consent_required", "standalone: no consents"
        doc = profile_for(Seeded("h", "u1", "m1", "op1", "c1", "d", "s"))
        assert profile.store(doc, version=1, sha256=protocol.sha256_hex(doc))["ok"]
        assert real.check_request("GET", "/media/snapshot/a.jpg", OWNER)


# ── commands ──────────────────────────────────────────────────────────────────
def handler(**hooks):
    from sentrix.link.commands import CommandHandler, CommandHooks
    from sentrix.storage import link_store
    pipeline = hooks.pop("pipeline", FakePipeline())
    channels = hooks.pop("channels", FakeChannels())
    sos = hooks.pop("sos", FakeSosService())
    wiped = []
    built = CommandHandler(CommandHooks(get_pipeline=lambda: pipeline,
                                        channels=lambda: channels,
                                        wipe_token=lambda: wiped.append(True),
                                        uptime=lambda: 12.5, **hooks),
                           audit=link_store.record_remote, sos=lambda: sos, clock=lambda: NOW)
    return built, SimpleBag(pipeline=pipeline, channels=channels, sos=sos, wiped=wiped)


def allow_team_phones(monkeypatch):
    """Both send switches on, test mode on, the three test numbers
    allowlisted: the guard then allows them (the messages still reach only
    the recording channels fake)."""
    monkeypatch.setenv("SENTRIX_LIVE_ALERTS", "1")
    monkeypatch.setenv("SENTRIX_MODEM_LIVE", "1")
    monkeypatch.setenv("SENTRIX_TEST_RECIPIENTS", f"{OWNER_PHONE},{CONTACT_PHONE},{DESK_PHONE}")


class SimpleBag:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def command(name, args=None, actor=OWNER, **extra):
    message = {"id": f"cmd-{name}-{time.monotonic_ns()}", "name": name, "args": args or {},
               "actor": actor.to_dict() if isinstance(actor, Actor) else actor}
    message.update(extra)
    return message


class TestCommandRules:
    def test_unknown_expired_and_malformed(self, temp_db):
        h, _ = handler()
        assert h.handle(command("shell.exec"))["code"] == "unknown_command"
        assert h.handle(command("diag.ping", expires_at=(NOW - 1) * 1000))["code"] == "timeout"
        assert h.handle(command("diag.ping", actor={"type": "company_admin"}))["code"] == \
            "invalid_args"
        ping = h.handle(command("diag.ping"))
        assert ping["ok"] and ping["result"]["pong"] and ping["result"]["uptime_s"] == 12.5

    def test_every_command_is_audited_without_secrets(self, temp_db):
        from sentrix.storage import link_store
        h, _ = handler()
        h.handle(command("sos.respond", {"request_id": "nope", "action": "decline",
                                         "pin": "135790"}))
        h.handle(command("shell.exec"))
        rows = link_store.recent_remote(10)
        assert [r["target"] for r in rows] == ["shell.exec", "sos.respond"]
        assert rows[0]["allowed"] is False and rows[0]["reason"] == "unknown_command"
        assert rows[1]["user_id"] == "u1" and rows[1]["role"] == "owner"
        assert all("135790" not in str(r) for r in rows), "a PIN is never logged"

    def test_sos_respond_roles(self, temp_db):
        h, bag = handler()
        request = bag.sos.open()
        assert h.handle(command("sos.respond", {"request_id": request["id"],
                                                "action": "decline"}, actor=DESK))["code"] == \
            "forbidden_role"
        assert h.handle(command("sos.respond", {"request_id": request["id"],
                                                "action": "checking"}, actor=CONTACT))["code"] == \
            "forbidden_role", "a contact may only ask for help"
        assert h.handle(command("sos.respond", {"request_id": request["id"],
                                                "action": "dance"}))["code"] == "invalid_args"
        assert h.handle(command("sos.respond", {"request_id": request["id"], "action": "approve",
                                                "authority": "NAVY"}))["code"] == "invalid_args"
        ok = h.handle(command("sos.respond", {"request_id": request["id"], "action": "approve",
                                              "authority": "fire"}, actor=CONTACT))
        assert ok["ok"] and ok["result"]["request"]["state"] == "APPROVED"
        assert bag.sos.calls[-1][2:6] == ("approve", "contact", "contact_page", "FIRE")

    def test_sos_update_only_cloud_desk_contact(self, temp_db):
        h, bag = handler()
        request = bag.sos.open()
        h.handle(command("sos.respond", {"request_id": request["id"], "action": "approve"}))
        assert h.handle(command("sos.update", {"request_id": request["id"],
                                               "event": "acknowledge"}))["code"] == \
            "forbidden_role", "an owner cannot acknowledge for the Desk"
        assert h.handle(command("sos.update", {"request_id": request["id"],
                                               "event": "cancel"},
                                actor=Actor.cloud()))["code"] == "invalid_args"
        ok = h.handle(command("sos.update", {"request_id": request["id"],
                                             "event": "delivered"}, actor=Actor.cloud()))
        assert ok["ok"] and ok["result"]["request"]["state"] == "DISPATCHED"

    def test_verdicts(self, temp_db):
        from sentrix.storage import database
        from sentrix.pipeline.incidents import IncidentManager
        incident, _ = IncidentManager().update(3, 0.5, "weapon", now=0.0, features={},
                                               reason="r", trigger={})
        database.save_incident(incident, "s1")
        h, _ = handler()
        assert h.handle(command("verdict.set", {"incident_id": incident.id,
                                                "verdict": "MAYBE"}))["code"] == "invalid_args"
        assert h.handle(command("verdict.set", {"incident_id": incident.id, "verdict": "TRUE"},
                                actor=DESK))["code"] == "forbidden_role"
        assert h.handle(command("verdict.set", {"incident_id": incident.id,
                                                "verdict": "TRUE", "note": "real"}))["ok"]
        row = database.recent_incidents()[0]
        assert row["verdict"] == "TRUE" and row["verdict_by"] == "portal:u1"
        assert h.handle(command("verdict.set", {"incident_id": "missing",
                                                "verdict": "TRUE"}))["code"] == "not_found"
        assert h.handle(command("verdict.set", {"incident_id": incident.id,
                                                "verdict": "FALSE"}, actor=MEMBER))["ok"]
        assert database.recent_incidents()[0]["verdict"] == "FALSE"

    def test_mode_disarm_needs_the_pin(self, temp_db):
        from sentrix import mode
        from test_link_support import PIN
        h, _ = handler()
        assert h.handle(command("mode.set", {"armed": "yes"}))["code"] == "invalid_args"
        armed = h.handle(command("mode.set", {"armed": True}, actor=MEMBER))
        assert armed["ok"] and armed["result"]["mode"]["armed"] and mode.is_armed()
        assert h.handle(command("mode.set", {"armed": False}))["code"] == "pin_invalid"
        assert mode.is_armed()
        assert h.handle(command("mode.set", {"armed": False, "pin": PIN}))["ok"]
        assert not mode.is_armed()

    def test_siren_test_owner_step_up_once_a_minute(self, temp_db):
        h, bag = handler()
        assert h.handle(command("siren.test", actor=MEMBER))["code"] == "forbidden_role"
        assert h.handle(command("siren.test", actor=STALE_OWNER))["code"] == "step_up_required"
        first = h.handle(command("siren.test"))
        assert first["ok"] and first["result"]["seconds"] == 1.0
        second = h.handle(command("siren.test"))
        assert second["code"] == "rate_limited" and second["result"]["retry_after"] > 0
        assert bag.pipeline.siren_calls == [1.0], "1 s, at most once a minute"

    def test_siren_limiter_is_shared_with_the_lc_route(self, temp_db):
        from sentrix.io.actuators import siren_test
        h, bag = handler()
        assert siren_test(bag.pipeline.siren, actor=Actor.device_admin())["ok"]
        assert h.handle(command("siren.test"))["code"] == "rate_limited"

    def test_sms_only_to_the_profile_allowlist(self, temp_db, monkeypatch):
        from sentrix.link import profile, protocol
        from test_link_support import Seeded, profile_for
        doc = profile_for(Seeded("h", "u1", "m1", "op1", "c1", "d", "s"))
        profile.store(doc, version=1, sha256=protocol.sha256_hex(doc))
        h, bag = handler()
        stranger = h.handle(command("sms.send", {"to": "+919800000000", "text": "hi"},
                                    actor=Actor.cloud()))
        assert stranger["code"] == "not_allowed_recipient"
        assert h.handle(command("sms.send", {"to": CONTACT_PHONE, "text": "hi"},
                                actor=MEMBER))["code"] == "forbidden_role"
        dry = h.handle(command("sms.send", {"to": CONTACT_PHONE, "text": "hi"},
                               actor=Actor.cloud()))
        assert dry["code"] == "not_allowed_recipient" and "guard" in dry["message"], \
            "the send guard refuses in a dry run (the default)"
        assert bag.channels.queued == []

    def test_sms_goes_to_the_router_when_the_guard_allows(self, temp_db, monkeypatch):
        from sentrix.link import profile, protocol
        from test_link_support import Seeded, profile_for
        doc = profile_for(Seeded("h", "u1", "m1", "op1", "c1", "d", "s"))
        profile.store(doc, version=1, sha256=protocol.sha256_hex(doc))
        allow_team_phones(monkeypatch)
        h, bag = handler()
        for number, ref in ((OWNER_PHONE, "owner"), (CONTACT_PHONE, "contact:c1"),
                            (DESK_PHONE, "desk")):
            sent = h.handle(command("sms.send", {"to": number, "text": "SOS " * 300,
                                                 "request_id": "r1"}, actor=Actor.cloud()))
            assert sent["ok"], sent
        refs = [r.ref for _k, r, _m in bag.channels.queued]
        assert refs == ["owner", "contact:c1", "desk"]
        assert all(r.channel == "modem_sms" for _k, r, _m in bag.channels.queued)
        assert len(bag.channels.queued[0][2].text) <= 612

    def test_sms_daily_cap_survives_a_restart(self, temp_db, monkeypatch):
        from datetime import datetime, timedelta, timezone
        from sentrix.alerting import guard
        from sentrix.link import profile, protocol
        from sentrix.storage import link_store
        from test_link_support import Seeded, profile_for
        doc = profile_for(Seeded("h", "u1", "m1", "op1", "c1", "d", "s"))
        profile.store(doc, version=1, sha256=protocol.sha256_hex(doc))
        allow_team_phones(monkeypatch)
        for _ in range(30):
            link_store.record_remote(kind="command", target="sms.send", allowed=True)
        h, bag = handler()
        capped = h.handle(command("sms.send", {"to": OWNER_PHONE, "text": "x"},
                                  actor=Actor.cloud()))
        assert capped["code"] == "daily_cap" and capped["status"] == 429
        since = datetime.now(timezone.utc) - timedelta(days=1)
        assert link_store.count_remote(kind="command", target="sms.send", since=since) == 30

    def test_unpair_and_rotate_need_owner_step_up(self, temp_db):
        from sentrix.link import profile, protocol
        from test_link_support import Seeded, profile_for
        doc = profile_for(Seeded("h", "u1", "m1", "op1", "c1", "d", "s"))
        profile.store(doc, version=1, sha256=protocol.sha256_hex(doc))
        h, bag = handler()
        assert h.handle(command("token.rotate", actor=STALE_OWNER))["code"] == "step_up_required"
        assert h.handle(command("token.rotate"))["ok"]
        assert h.handle(command("unpair", actor=MEMBER))["code"] == "forbidden_role"
        assert h.handle(command("unpair", actor=STALE_OWNER))["code"] == "step_up_required"
        assert profile.current() is not None and bag.wiped == []
        done = h.handle(command("unpair"))
        assert done["ok"] and bag.wiped == [True] and profile.current() is None

    def test_demo_commands_only_in_test_mode(self, temp_db):
        h, bag = handler(test_mode=lambda: False)
        assert h.handle(command("demo.trigger", {"incident_type": "weapon"}))["code"] == \
            "test_mode_only"
        h2, bag2 = handler()
        assert h2.handle(command("demo.trigger", {"incident_type": "sabotage"}))["code"] == \
            "invalid_args"
        assert h2.handle(command("demo.trigger", {"incident_type": "fire"}))["ok"]
        assert h2.handle(command("demo.close_incident"))["ok"]
        assert bag2.sos.calls == [("demo_trigger", "fire", "owner"),
                                  ("demo_close_incident", "owner")]

    def test_a_crashing_handler_is_an_internal_error(self, temp_db):
        class Broken(FakeSosService):
            def respond(self, *a, **k):
                raise RuntimeError("boom")
        h, _ = handler(sos=Broken())
        result = h.handle(command("sos.respond", {"request_id": "r", "action": "approve"}))
        assert result["code"] == "internal" and result["status"] == 500
