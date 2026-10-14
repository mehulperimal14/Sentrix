"""Regressions for the bugs the live smoke run found (docs/7 §17).

The Portal and the device ran as two real processes on the laptop, linked
over a real socket, and were driven like a browser. These tests pin what that
run showed and the in-process suites could not:

* the Portal committed a request's database work only *after* sending the
  response, so a browser following the sign-in redirect at once could arrive
  before its session existed (the in-process ASGI client waits for the whole
  request, which hid it);
* a Portal restart read its own downtime as the device's lost heartbeat
  (tamper signal, cloud takeover, contacts paged) although the device came
  back within a second;
* the LC showed an outbox depth of 0 while events queued during an outage;
* the scene model asked huggingface.co for a revision on every start even
  with the base weights cached (the fire-before-expiry rule is pinned in
  ``test_sos_engine.py``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest


# ── the Portal commits before it answers ─────────────────────────────────────
def _asgi_call(app, method: str, path: str, *, body: bytes = b"", headers=(),
               on_start=None):
    """Drive ``app`` as a server does; ``on_start(message)`` runs when the
    response headers go out, i.e. when a browser may already follow them."""
    messages: list[dict] = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "server": ("testserver", 80),
             "client": ("198.51.100.9", 50000),
             "headers": [(b"host", b"testserver"), *headers]}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start" and on_start is not None:
            on_start(message)
        messages.append(message)

    async def run():
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)

    asyncio.run(run())
    return messages


def test_the_sign_in_session_exists_before_the_redirect_is_sent(portal_app):
    from urllib.parse import urlencode

    from sentrix_portal.models import UserSession
    from test_acct_support import PASSWORD, add_user
    add_user(portal_app, username="riya", role="owner")
    token = "t" * 32
    seen: list[int] = []

    def count_sessions(message):
        with portal_app.state.db.session() as db:      # another connection
            seen.append(db.query(UserSession).count())

    body = urlencode({"username": "riya", "password": PASSWORD,
                      "csrf_token": token}).encode()
    messages = _asgi_call(portal_app, "POST", "/login", body=body, headers=[
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"accept", b"text/html"), (b"cookie", f"portal_csrf={token}".encode())],
        on_start=count_sessions)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 303, start
    assert seen == [1], "the session row must be committed when the redirect leaves"


def test_every_portal_route_takes_the_function_scoped_session(portal_app):
    from fastapi.routing import APIRoute, APIWebSocketRoute

    from sentrix_portal.db import get_db

    def walk(dependant, route):
        for sub in dependant.dependencies:
            if sub.call is get_db:
                assert sub.scope == "function", \
                    f"{route.path}: get_db must commit before the response"
            walk(sub, route)

    def routes(container):
        for route in container:
            included = getattr(route, "original_router", None)   # FastAPI's lazy include
            if included is not None:
                yield from routes(included.routes)
            elif isinstance(route, (APIRoute, APIWebSocketRoute)):
                yield route

    checked = 0
    for route in routes(portal_app.routes):
        walk(route.dependant, route)
        checked += 1
    assert checked > 50


# ── a Portal restart is not a lost heartbeat ─────────────────────────────────
def test_a_portal_restart_gives_devices_the_full_limit_to_reconnect(portal_app):
    from sentrix_portal.models import Device, utcnow
    from sentrix_portal.services.hub import LinkHub
    from test_acct_support import add_device
    device_id = add_device(portal_app, serial="SX5-000009", state="CLAIMED")
    with portal_app.state.db.session() as db:
        row = db.get(Device, device_id)
        row.online = True                              # as the Portal left it
        row.last_seen_at = utcnow() - timedelta(minutes=10)
    hub = LinkHub(portal_app)                          # the Portal starts again
    lost, _ = hub._mark_offline(utcnow() + timedelta(seconds=5), 45.0)
    assert lost == [], "the Portal's own downtime is not the device's"
    with portal_app.state.db.session() as db:
        assert db.get(Device, device_id).online
    lost, _ = hub._mark_offline(hub.started_at + timedelta(seconds=46), 45.0)
    assert lost == [device_id], "a device that never came back still goes offline"


# ── the outbox depth while offline ───────────────────────────────────────────
def test_the_outbox_depth_is_refreshed_while_the_link_is_down(monkeypatch, tmp_path):
    from sentrix.link import client as link_client
    from sentrix.storage import link_store
    depths = iter([0, 2, 3, 3, 3, 3, 3, 3])
    monkeypatch.setattr(link_store, "depth", lambda: next(depths))
    monkeypatch.setattr(link_client, "OFFLINE_DEPTH_SECONDS", 0.02)
    pushed: list[int] = []
    state = SimpleNamespace(update=lambda **kw: pushed.append(kw["link"]["outbox_depth"]))
    cfg = SimpleNamespace(portal_url="", heartbeat_seconds=15.0)
    service = link_client.LinkService(SimpleNamespace(settings=None, extras={}, state=state),
                                      cfg=cfg, identity_dir=tmp_path)

    async def run():
        service._kick = asyncio.Event()
        await service._offline_wait(0.1)

    asyncio.run(run())
    assert service.status()["outbox_depth"] == 3
    assert pushed and pushed[-1] == 3, "the LC's link block shows the queued events"


def test_a_kick_still_ends_the_offline_wait_at_once(monkeypatch, tmp_path):
    import time

    from sentrix.link import client as link_client
    from sentrix.storage import link_store
    monkeypatch.setattr(link_store, "depth", lambda: 0)
    cfg = SimpleNamespace(portal_url="", heartbeat_seconds=15.0)
    service = link_client.LinkService(SimpleNamespace(settings=None, extras={}, state=None),
                                      cfg=cfg, identity_dir=tmp_path)

    async def run():
        service._kick = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, service._kick.set)
        started = time.monotonic()
        await service._offline_wait(30.0)
        return time.monotonic() - started

    assert asyncio.run(run()) < 5.0


# ── the scene model's cached base weights ────────────────────────────────────
def test_cached_base_weights_load_without_asking_the_hub(monkeypatch, tmp_path):
    pytest.importorskip("timm")
    import huggingface_hub

    from sentrix.perception import scene
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"x")
    asked: list[tuple[str, str]] = []

    def fake_cache(repo, filename, **kwargs):
        asked.append((repo, filename))
        return str(weights) if filename == "model.safetensors" else None

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", fake_cache)
    assert scene._cached_timm_weights("vit_base_patch14_dinov2.lvd142m") == str(weights)
    assert asked[0] == ("timm/vit_base_patch14_dinov2.lvd142m", "model.safetensors")

    made: dict = {}
    import timm
    monkeypatch.setattr(timm, "create_model", lambda name, **kw: made.update(kw) or "net")
    assert scene.build_scene_model("timm:vit_base_patch14_dinov2.lvd142m", 5,
                                   pretrained=True, image_size=224) == "net"
    assert made["pretrained_cfg_overlay"] == {"file": str(weights)}
    assert made["pretrained"] is True

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lambda *a, **k: None)
    made.clear()
    scene.build_scene_model("timm:vit_base_patch14_dinov2.lvd142m", 5, pretrained=True)
    assert "pretrained_cfg_overlay" not in made, "no cache: timm downloads as before"
