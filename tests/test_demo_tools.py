"""Demo tooling (docs/7 §15.3): tools/demo_reset.py, tools/demo_run.py, the
demo reel camera (sentrix/io/demo_reel.py) and the admin-only scripted trigger.

Everything runs on temporary directories: no demo process is started, no
camera or microphone is opened, nothing is sent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from sentrix.paths import ROOT_DIR

if str(ROOT_DIR / "tools") not in sys.path:
    sys.path.append(str(ROOT_DIR / "tools"))

import demo_reset  # noqa: E402
import demo_run  # noqa: E402
from test_acct_support import Browser  # noqa: E402
from test_sos_routes import tunnel, world  # noqa: E402,F401  (fixture)


@pytest.fixture
def demo(tmp_path):
    return demo_reset.reset(tmp_path / "demo")


def portal_for(world_: demo_reset.DemoWorld):
    from sentrix_portal.app import create_app
    from sentrix_portal.settings import PortalSettings
    paths = demo_reset.layout(world_.directory)
    settings = PortalSettings(
        database_url=f"sqlite:///{(paths['portal'] / 'portal.db').as_posix()}",
        data_dir=str(paths["portal"]), test_mode=True,
        base_url=world_.info["portal_url"]).checked()
    return create_app(settings)


# ── demo_reset ────────────────────────────────────────────────────────────────
class TestDemoReset:
    def test_a_finished_household_with_a_claimed_device(self, demo):
        from sentrix_portal import security
        from sentrix_portal.models import (Camera, Device, EmergencyContact, Household,
                                           User)
        from sentrix_portal.services import accounts
        app = portal_for(demo)
        with app.state.db.session() as db:
            household = db.query(Household).one()
            assert household.display_name == "Riya Sharma (demo)"
            assert household.onboarding_completed_at is not None
            assert household.sos_pin_hash and household.duress_pin_hash
            assert household.safety_defaults is True
            owner = accounts.household_owner(db, household.id)
            assert all(accounts.onboarding_done(db, household, owner).values())
            device = db.query(Device).one()
            assert device.state == "CLAIMED" and device.household_id == household.id
            assert db.query(Camera).filter_by(device_id=device.id).one().room_label == \
                "Living room"
            contact = db.query(EmergencyContact).one()
            assert contact.consent_status == "CONFIRMED"
            assert accounts.confirmed_contacts(db, household.id) == [contact]
            assert {u.role for u in db.query(User)} == {"company_admin", "operator", "owner"}
            staff = db.query(User).filter(User.role != "owner").all()
            assert all(u.totp_enabled and u.totp_secret_enc for u in staff)
            assert security.verify_audit_chain(db)["ok"]
            profile = accounts.build_profile(db, household.id, device.id)
            assert profile["test_mode"] is True and profile["version"] >= 1
            assert profile["contacts"][0]["name"] == "Prashant (demo contact)"
            # the demo PINs verify against what the device would receive
            assert security.verify_pin(profile["sos"]["pin_hash"], demo.secrets["pins"]["sos_pin"])
        identity = json.loads((demo.directory / "device" / "link" / "identity.json")
                              .read_text(encoding="utf-8"))
        assert identity["device_id"] == demo.info["device_id"]
        assert (demo.directory / "label.html").exists()

    def test_people_sign_in_with_the_printed_credentials(self, demo):
        import pyotp

        from sentrix_portal import security
        app = portal_for(demo)
        owner = Browser(app)
        response = owner.login(demo.secrets["owner"]["username"],
                               demo.secrets["owner"]["password"])
        assert response.status_code == 303 and response.headers["location"].endswith("/home")
        for key in ("admin", "operator"):
            staff = demo.secrets[key]
            browser = Browser(app, ip=f"10.0.0.{len(key)}")
            browser.login(staff["username"], staff["password"])
            security._totp_last_step.clear()
            browser.get("/account/2fa")
            done = browser.post("/account/2fa", {"action": "verify",
                                                 "code": pyotp.TOTP(staff["totp_secret"]).now()})
            assert done.status_code == 303, done.text

    def test_secrets_are_shown_once_not_stored(self, demo):
        stored = (demo.directory / "demo.json").read_text(encoding="utf-8")
        for value in (demo.secrets["owner"]["password"], demo.secrets["admin"]["password"],
                      demo.secrets["admin"]["totp_secret"], demo.secrets["operator"]["totp_secret"],
                      demo.secrets["pins"]["sos_pin"], demo.secrets["device"]["claim_code"]):
            assert value not in stored
        info = json.loads(stored)
        # only the demo device's own console secrets are kept (demo_run needs them)
        assert set(info["device_secrets"]) == {"lc_password", "session_secret", "evidence_key"}
        assert info["env"]["SENTRIX_TEST_MODE"] == "1" and info["env"]["SENTRIX_LIVE_ALERTS"] == "0"
        text = demo_reset.describe(demo)
        assert demo.secrets["admin"]["totp_secret"] in text and "TEST MODE" in text

    def test_live_steps_and_activation(self, tmp_path):
        from sentrix_portal.models import Device, Household, User
        from sentrix_portal.services import accounts
        world_ = demo_reset.reset(tmp_path / "d", live_steps=3, activation=True,
                                  safety_net=False)
        assert world_.info["live_steps"] == [5, 6, 7] and "pins" not in world_.secrets
        app = portal_for(world_)
        with app.state.db.session() as db:
            household = db.query(Household).one()
            assert household.onboarding_step == 5 and household.onboarding_completed_at is None
            assert household.sos_pin_hash is None and household.safety_defaults is False
            assert db.query(Device).one().state == "ASSIGNED"
            owner = db.query(User).filter_by(role="owner").one()
            assert owner.status == "pending_activation" and owner.password_hash is None
            found = accounts.activation_for_typed(db, owner.username,
                                                  world_.secrets["owner"]["activation_code"])
            assert found is not None
        two = demo_reset.reset(tmp_path / "d2", live_steps=2)
        assert two.info["live_steps"] == [6, 7]

    def test_reset_wipes_only_a_demo_directory(self, tmp_path, demo):
        first = demo.info["device_id"]
        again = demo_reset.reset(demo.directory)
        assert again.info["device_id"] != first
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        (foreign / "sentrix.db").write_text("not mine")
        with pytest.raises(demo_reset.DemoRefused):
            demo_reset.reset(foreign)
        assert (foreign / "sentrix.db").read_text() == "not mine"
        for refused in (ROOT_DIR / "var", ROOT_DIR):
            with pytest.raises(demo_reset.DemoRefused):
                demo_reset.check_directory(refused)

    def test_bad_arguments(self, tmp_path):
        with pytest.raises(ValueError):
            demo_reset.reset(tmp_path / "x", live_steps=4)
        with pytest.raises(ValueError):
            demo_reset.reset(tmp_path / "y", window_seconds=30)
        assert demo_reset.main(["--dir", str(ROOT_DIR / "var")]) == 2


# ── demo_run ──────────────────────────────────────────────────────────────────
class TestDemoRunEnvironment:
    HOSTILE = {"SENTRIX_LIVE_ALERTS": "1", "SENTRIX_TWILIO_ENABLED": "1",
               "SENTRIX_TELEGRAM_LIVE": "1", "SENTRIX_EMAIL_LIVE": "1",
               "TWILIO_AUTH_TOKEN": "live-token", "TELEGRAM_BOT_TOKEN": "bot",
               "SENTRIX_TEST_MODE": "0", "PORTAL_TEST_MODE": "0", "CAMERA_SOURCES": "0",
               "SENTRIX_AUDIO": "1", "SENTRIX_USER_PHONE": "+919999999999",
               "SENTRIX_TEST_RECIPIENTS": "+919812345610", "SENTRIX_VAR_DIR": ""}

    def test_live_switches_are_forced_off_and_keys_blanked(self, demo):
        reel = demo_run.write_reel(demo.directory)
        for env in (demo_run.device_env(demo.info, reel, base=self.HOSTILE),
                    demo_run.portal_env(demo.info, base=self.HOSTILE)):
            demo_run.assert_safe(env)
            assert env["SENTRIX_TEST_MODE"] == env["PORTAL_TEST_MODE"] == "1"
            for key in ("SENTRIX_LIVE_ALERTS", "SENTRIX_TWILIO_ENABLED",
                        "SENTRIX_TELEGRAM_LIVE", "SENTRIX_EMAIL_LIVE", "SENTRIX_MODEM_LIVE"):
                assert env[key] == "0"
            assert env["TWILIO_AUTH_TOKEN"] == env["TELEGRAM_BOT_TOKEN"] == ""
            assert env["SENTRIX_USER_PHONE"] == ""
            # the allowlist comes from the environment only
            assert env["SENTRIX_TEST_RECIPIENTS"] == "+919812345610"
            assert env["SENTRIX_SOS_DEMO_WINDOW_SECONDS"] == "90"
            assert Path(env["SENTRIX_VAR_DIR"]).resolve() != (ROOT_DIR / "var").resolve()
        device = demo_run.device_env(demo.info, reel, base={})
        assert device["CAMERA_SOURCES"] == f"demo-reel:{reel}" and device["SENTRIX_AUDIO"] == "0"
        assert device["SENTRIX_SOS_ENABLED"] == device["SENTRIX_LINK_ENABLED"] == "1"
        assert device["SENTRIX_PORTAL_URL"] == "http://127.0.0.1:8100"
        assert device["SENTRIX_PASSWORD"] == demo.info["device_secrets"]["lc_password"]
        portal = demo_run.portal_env(demo.info, base={})
        assert portal["PORTAL_WORKERS"] == "1" and portal["SENTRIX_LINK_ENABLED"] == "0"
        # different hosts: the portal_session and lc_session cookies never meet
        assert "portal.localhost" in demo.info["portal_url"]
        assert "127.0.0.1" in demo.info["lc_url"]

    def test_assert_safe_refuses_a_webcam_a_mic_or_live_alerts(self, demo):
        reel = demo_run.write_reel(demo.directory)
        good = demo_run.device_env(demo.info, reel, base={})
        for change in ({"CAMERA_SOURCES": "0"}, {"CAMERA_SOURCES": "rtsp://cam/1"},
                       {"SENTRIX_AUDIO": "1"}, {"SENTRIX_LIVE_ALERTS": "1"},
                       {"SENTRIX_TEST_MODE": "0"}, {"SENTRIX_VAR_DIR": str(ROOT_DIR / "var")}):
            with pytest.raises(RuntimeError):
                demo_run.assert_safe({**good, **change})

    def test_the_reel_never_holds_the_test_video(self, demo):
        from sentrix.io import demo_reel
        reel = json.loads(demo_run.write_reel(demo.directory).read_text(encoding="utf-8"))
        files = [Path(reel["idle"]["file"]).name] + [Path(s["file"]).name
                                                     for s in reel["scenes"].values()]
        assert not set(files) & demo_reel.test_split_files()
        assert all((demo_run.VIDEOS / name).exists() for name in files)
        with pytest.raises(ValueError):
            demo_reel.refuse_test_video(demo_run.VIDEOS /
                                        "WhatsApp Video 2026-09-19 at 7.04.16 PM (2).mp4")
        held_out = {name for name, s in demo_run.SCENES.items() if s["held_out"]}
        assert held_out == {"gun", "fire"}
        assert all(demo_run.SCENES[n]["split"] == "val" for n in held_out)

    def test_scene_command_writes_the_control_file(self, demo, capsys):
        from sentrix.io import demo_reel
        assert demo_run.main(["--dir", str(demo.directory), "scene", "gun"]) == 0
        token, scene = demo_reel.read_control(demo.directory / demo_run.CONTROL_FILE)
        assert scene == "gun" and token
        assert demo_run.main(["scene", "--dir", str(demo.directory), "nope"]) == 2
        assert demo_run.main(["clips"]) == 0
        assert "wa02 is never used" in capsys.readouterr().out

    def test_commands_need_a_demo(self, tmp_path):
        assert demo_run.main(["--dir", str(tmp_path / "none"), "close"]) == 2


# ── the reel camera ──────────────────────────────────────────────────────────
def _clip(path: Path, colour: tuple[int, int, int], frames: int, size=(64, 48)) -> Path:
    import cv2
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, size)
    for _ in range(frames):
        writer.write(np.full((size[1], size[0], 3), colour, dtype=np.uint8))
    writer.release()
    return path


class TestDemoReel:
    @pytest.fixture
    def reel(self, tmp_path, monkeypatch):
        from sentrix.io import demo_reel
        monkeypatch.setattr(demo_reel, "CONTROL_POLL_SECONDS", 0.0)
        idle = _clip(tmp_path / "idle.avi", (0, 0, 200), 6)
        scene = _clip(tmp_path / "scene.avi", (200, 0, 0), 4, size=(80, 60))
        control = tmp_path / "scene.txt"
        demo_reel.write_control(control, "gun")          # stale: must not replay
        path = tmp_path / "reel.json"
        path.write_text(json.dumps({"control": str(control),
                                    "idle": {"file": str(idle)},
                                    "scenes": {"gun": {"file": str(scene)}}}))
        return path, control

    @staticmethod
    def _colour(source) -> str:
        ok, frame = source.read()
        assert ok and frame.shape[:2] == (48, 64), "every frame has the idle size"
        return "red" if frame[..., 2].mean() > 100 else "blue"

    def test_idle_loops_and_a_scene_plays_once(self, reel):
        from sentrix.io import demo_reel
        from sentrix.io.sources import build_source
        path, control = reel
        source = build_source(f"demo-reel:{path}")
        assert isinstance(source, demo_reel.DemoReelSource)
        assert [self._colour(source) for _ in range(14)] == ["red"] * 14, "stale token ignored"
        demo_reel.write_control(control, "gun")
        colours = [self._colour(source) for _ in range(10)]
        assert colours[:4] == ["blue"] * 4 and colours[4:] == ["red"] * 6
        assert source.scene == "idle" and [p[0] for p in source.plays] == ["gun"]
        demo_reel.write_control(control, "unknown")
        assert self._colour(source) == "red"
        source.release()

    def test_refuses_the_test_video_and_bad_specs(self, tmp_path):
        from sentrix.io import demo_reel
        bad = tmp_path / "reel.json"
        bad.write_text(json.dumps({"idle": {"file": "WhatsApp Video 2026-09-19 at "
                                                    "7.04.16 PM (2).mp4"}}))
        with pytest.raises(ValueError):
            demo_reel.DemoReelSource(bad)
        bad.write_text(json.dumps({"idle": {"file": "wa02.mp4"}}))
        with pytest.raises(ValueError):
            demo_reel.load_reel(bad)
        bad.write_text(json.dumps({"idle": {"file": "a.mov", "start": 5, "end": 2}}))
        with pytest.raises(ValueError):
            demo_reel.load_reel(bad)
        with pytest.raises(ValueError):
            demo_reel.build("demo-reel:")


# ── the scripted trigger is the device admin's ───────────────────────────────
class TestScriptedTrigger:
    def test_device_admin_only(self, world):  # noqa: F811
        refused = world.client.post("/api/sos/demo/trigger", json={"incident_type": "weapon"},
                                    headers=tunnel("owner"))
        assert refused.status_code == 403
        ok = world.client.post("/api/sos/demo/trigger", json={"incident_type": "weapon"},
                               headers=world.csrf)
        assert ok.status_code == 200 and ok.json()["message"] == "SIMULATED DETECTION"
        from sentrix.storage import requests
        opened = requests.list_requests(1)[0]
        assert opened["flags"]["simulated"]
        assert "SIMULATED DETECTION" in (opened.get("sos_text_full") or "")

    def test_refused_outside_test_mode(self, world, monkeypatch):  # noqa: F811
        monkeypatch.setattr(world.engine, "_test_mode", lambda: False)
        response = world.client.post("/api/sos/demo/trigger", json={"incident_type": "fire"},
                                     headers=world.csrf)
        assert response.json()["code"] == "test_mode_only"
