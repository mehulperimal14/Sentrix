"""What a newcomer meets on a fresh clone: bundled data, camera choice, login.

The repository ships the custom dataset and its videos so every evaluation and
a camera-free run work out of the box; these tests keep it that way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from sentrix.evaluation.custom_dataset import load_frames, videos_for
from sentrix.io.sources import FileSource, build_source
from sentrix.paths import ROOT_DIR

BUNDLED = "training/datasets/sentrix_custom/videos/gun.mov"


class TestBundledData:
    def test_every_labelled_frame_and_video_ships(self, monkeypatch):
        monkeypatch.delenv("SENTRIX_CUSTOM_DATASET", raising=False)
        monkeypatch.delenv("SENTRIX_CUSTOM_VIDEOS", raising=False)
        frames = load_frames()
        assert len(frames) == 765 and all(f.path.exists() for f in frames)
        assert all(v.path().exists() for v in videos_for("all"))


class TestCameraChoice:
    def test_bundled_video_as_camera_from_any_directory(self, tmp_path, monkeypatch):
        """A relative CAMERA_SOURCES path resolves from the repository root and
        loops, instead of opening a webcam or freezing on the last frame."""
        monkeypatch.chdir(tmp_path)
        source = build_source(BUNDLED)
        try:
            assert isinstance(source, FileSource) and source._loop
            assert Path(source.path) == ROOT_DIR / BUNDLED
        finally:
            source.release()


class TestFirstLogin:
    def test_unset_password_sends_the_browser_to_the_login_page(self, monkeypatch):
        from sentrix.web.routes import require_auth
        monkeypatch.setenv("SENTRIX_PASSWORD", "")
        with pytest.raises(HTTPException) as caught:
            asyncio.run(require_auth(None))
        assert caught.value.status_code == 307
        assert caught.value.headers["Location"] == "/login"

    def test_login_page_explains_a_missing_password(self, monkeypatch):
        from fastapi.testclient import TestClient
        from sentrix.app import app
        monkeypatch.setenv("SENTRIX_PASSWORD", "")
        page = TestClient(app).get("/login")       # no lifespan: no pipeline, no camera
        assert page.status_code == 200
        assert "Set SENTRIX_PASSWORD in .env" in page.text


class TestReplayToolIsSideEffectFree:
    def test_replay_never_acts(self):
        """tools/replay.py used to build a live pipeline: webcam from
        CAMERA_SOURCES, mic on, and real SMS/siren on a weapon clip."""
        source = (ROOT_DIR / "tools" / "replay.py").read_text(encoding="utf-8")
        for stub in ("pipeline._act = _noop", "pipeline._submit = _noop",
                     "pipeline._save_open_incident = _noop", "source=_NullSource()"):
            assert stub in source


class TestFirstRunSecrets:
    def test_blank_secrets_are_generated_and_set_values_kept(self):
        from tools.setup_env import fill
        text, made = fill("SENTRIX_PASSWORD=\nSESSION_SECRET=keepme\nEVIDENCE_AES_KEY=\n")
        assert set(made) == {"SENTRIX_PASSWORD", "EVIDENCE_AES_KEY"}
        assert "SESSION_SECRET=keepme" in text
        assert f"SENTRIX_PASSWORD={made['SENTRIX_PASSWORD']}" in text
        assert len(made["EVIDENCE_AES_KEY"]) == 64

    def test_missing_keys_are_appended(self):
        from tools.setup_env import fill
        text, made = fill("CAMERA_SOURCES=0\n")
        assert set(made) == {"SENTRIX_PASSWORD", "SESSION_SECRET", "EVIDENCE_AES_KEY"}
        assert text.startswith("CAMERA_SOURCES=0\n")

    def test_template_has_every_generated_key_blank(self):
        from tools.setup_env import GENERATED
        template = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        for key in GENERATED:
            assert f"\n{key}=\n" in template, f"{key} must stay blank in .env.example"
